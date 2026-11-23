import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

import yt_dlp

from .delivery import DeliveryError, DownloadGuard, ResourceBlocked, ResourceClient, deliver, telegram_call

YOUTUBE_SESSION_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
YOUTUBE_COOKIE_DOMAINS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
ORIGINAL_FORMAT = "bestvideo*+bestaudio/best"
YOUTUBE_FORMAT = (
    "bestvideo*[ext=mp4][vcodec^=avc1][acodec~='^(none$|mp4a)']+bestaudio[ext=m4a][acodec^=mp4a]/"
    "best[ext=mp4][vcodec^=avc1][acodec^=mp4a]/"
    + ORIGINAL_FORMAT
)


def validate_url(raw):
    value = (raw or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("A valid HTTP(S) URL is required.")
    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith((".local", ".internal")):
        raise ValueError("Local network URLs are not accepted.")
    if re.search(r"[\x00-\x20]", value):
        raise ValueError("URL contains whitespace or control characters.")
    return value


def youtube_authentication(url, session):
    parts = urlparse(url)
    if not session or parts.scheme != "https" or parts.hostname not in YOUTUBE_SESSION_HOSTS:
        return None
    if parts.username or parts.password:
        return None

    if parts.hostname == "youtu.be":
        video_id = parts.path.strip("/")
    elif parts.path == "/watch":
        video_id = parse_qs(parts.query).get("v", [""])[0]
    else:
        video_path = re.fullmatch(r"/(?:shorts|live|embed)/([\w-]+)/*", parts.path, re.ASCII)
        video_id = video_path[1] if video_path else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return None

    rows = session.lstrip("\ufeff").splitlines()
    if not rows or not rows[0].startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        raise ValueError("ملف جلسة يوتيوب يجب أن يكون بصيغة Netscape.")
    cookies = []
    for row in rows[1:]:
        if not row or (row.startswith("#") and not row.startswith("#HttpOnly_")):
            continue
        fields = row.removeprefix("#HttpOnly_").split("\t")
        if len(fields) != 7:
            raise ValueError("ملف جلسة يوتيوب يحتوي على سطر غير صالح.")
        domain, include_subdomains, path, secure, expires, name, _ = fields
        domain = domain.lower()
        if domain.removeprefix(".") not in YOUTUBE_COOKIE_DOMAINS:
            continue
        fields[0] = domain
        if (include_subdomains != ("TRUE" if domain.startswith(".") else "FALSE")
                or secure not in {"TRUE", "FALSE"} or not path.startswith("/")
                or not re.fullmatch(r"[0-9]*", expires) or not name):
            raise ValueError("ملف جلسة يوتيوب يحتوي على سطر غير صالح.")
        fields[3] = "TRUE"  # Pass login cookies only over HTTPS.
        cookies.append("\t".join(fields))
    if not cookies:
        raise ValueError("ملف الجلسة لا يحتوي على بيانات خاصة بيوتيوب.")
    return (
        f"https://www.youtube.com/watch?v={video_id}",
        io.StringIO("# Netscape HTTP Cookie File\n" + "\n".join(cookies) + "\n"),
    )


def youtube_session_retryable(error):
    detail = str(error).casefold().replace("’", "'")
    return "[youtube]" in detail and any(phrase in detail for phrase in (
        "sign in to confirm", "sign in to verify", "login required", "this video is private",
        "video unavailable. this content isn't available",
        "this content isn't available, try again later",
    ))


def extract_media(url, options):
    completed_paths = []
    with yt_dlp.YoutubeDL({**options, "post_hooks": [completed_paths.append]}) as downloader:
        info = downloader.extract_info(url, download=True)
        final_path = completed_paths[-1] if completed_paths else info.get("filepath")
        if not final_path:
            final_path = downloader.prepare_filename(info)
    return info, Path(final_path)


def download_source(url, options, guard, youtube_session):
    try:
        return extract_media(url, options)
    except yt_dlp.utils.DownloadError as guest_error:
        if not (
            urlparse(url).hostname in YOUTUBE_SESSION_HOSTS
            and youtube_session_retryable(guest_error)
            and not guard.files
        ):
            raise
        try:
            authentication = youtube_authentication(url, youtube_session)
        except ValueError:
            print("YouTube session skipped because its configuration is invalid.", file=sys.stderr)
            authentication = None
        if authentication is None:
            raise DeliveryError(
                "يوتيوب طلب تسجيل الدخول قبل إتاحة التنزيل. إذا عندك الملف الأصلي، أرسل رابطه المباشر. "
                "إذا المقطع من قناتك، تقدر تصدّره من استوديو يوتيوب أو Google Takeout."
            ) from None
        authenticated_url, cookies = authentication
        print("YouTube rejected the guest request. Trying the configured YouTube session once.")
        with cookies:
            try:
                return extract_media(authenticated_url, {
                    **options, "cookiefile": cookies, "allowed_extractors": ["youtube"],
                })
            except yt_dlp.utils.DownloadError as session_error:
                if not youtube_session_retryable(session_error):
                    raise
                raise DeliveryError(
                    "يوتيوب لم يُتح تنزيل هذا المقطع باستخدام جلسة الحساب المحفوظة. إذا عندك الملف الأصلي، أرسل رابطه المباشر. "
                    "إذا المقطع من قناتك، تقدر تصدّره من استوديو يوتيوب أو Google Takeout."
                ) from None


def probe_media(media_path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=index,codec_type,codec_name,duration,start_time,width,height,nb_frames,sample_rate,channels,avg_frame_rate:stream_disposition=default",
            "-of", "json",
            str(media_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or "unknown media error").strip()[-400:]
        raise RuntimeError("The completed download is not a readable media file: " + detail)
    try:
        metadata = json.loads(result.stdout)
        video = next(stream for stream in metadata["streams"] if stream.get("codec_type") == "video")
    except (ValueError, KeyError, StopIteration):
        raise RuntimeError("The completed file has no readable video stream.") from None
    return metadata, video


def positive_seconds(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else 0
    except (TypeError, ValueError):
        return 0


def probe_duration(media_path):
    metadata, video = probe_media(media_path)
    # Audio/container duration can hide a video stream that lasts only a fraction of a second.
    duration = positive_seconds(video.get("duration")) or positive_seconds(metadata.get("format", {}).get("duration"))
    if not duration:
        raise RuntimeError("The completed video has no playable duration.")
    return duration


def validate_duration(media_path, source_duration):
    actual = probe_duration(media_path)
    expected = positive_seconds(source_duration)
    if expected > 1:
        tolerance = max(0.5, expected * 0.05)
        if actual + tolerance < expected:
            raise RuntimeError(
                f"Incomplete download detected: source is about {expected:.1f}s, "
                f"but the final file is only {actual:.1f}s."
            )
    return actual


def decode_audio(media_path, stream_index):
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin",
         "-i", str(media_path), "-map", f"0:{stream_index}", "-vn", "-f", "null", "-"],
        capture_output=True, text=True, timeout=900, check=False,
    )


def select_audio_stream(media_path, metadata):
    candidates = [stream for stream in metadata["streams"] if stream.get("codec_type") == "audio"]
    candidates.sort(key=lambda stream: (-int(stream.get("disposition", {}).get("default", 0)), stream["index"]))
    for stream in candidates:
        if decode_audio(media_path, stream["index"]).returncode == 0:
            print(f"Selected fully readable audio stream {stream['index']}.")
            return stream
        print(f"Rejected unreadable audio stream {stream['index']}.")
    if candidates:
        raise RuntimeError("The source audio is damaged; no distorted or silent replacement was sent.")
    return None


def audio_fingerprint(media_path, stream_index):
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-i", str(media_path), "-map", f"0:{stream_index}", "-vn", "-c:a", "copy",
         "-f", "hash", "-hash", "sha256", "-"],
        capture_output=True, text=True, timeout=900, check=False,
    )
    fingerprint = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"SHA256=[0-9a-fA-F]{64}", fingerprint):
        raise RuntimeError("Could not verify the original audio data.")
    return fingerprint


def mark_normal_playback(media_path, video):
    try:
        numerator, denominator = str(video.get("avg_frame_rate", "0/1")).split("/")
        frame_rate = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        frame_rate = 0
    if frame_rate <= 60:
        return
    config_path = Path(__file__).with_name("full-frame-rate.config")
    result = subprocess.run(
        ["exiftool", "-config", str(config_path), "-overwrite_original",
         "-Keys:FullFrameRatePlaybackIntent=1", str(media_path)],
        capture_output=True, text=True, timeout=180, check=False,
    )
    if result.returncode:
        raise RuntimeError("Could not set normal-speed playback for the phone album.")
    result = subprocess.run(
        ["exiftool", "-n", "-s3", "-Keys:FullFrameRatePlaybackIntent", str(media_path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode or result.stdout.strip() != "1":
        raise RuntimeError("Normal-speed playback metadata could not be verified.")
    print(f"Normal-speed album playback metadata verified; all {frame_rate:g} fps preserved.")


def prepare_delivery_media(media_path, source_duration):
    validate_duration(media_path, source_duration)
    metadata, video = probe_media(media_path)
    selected_audio = select_audio_stream(media_path, metadata)
    is_mp4 = media_path.suffix.lower() in {".mp4", ".mov", ".m4v"}
    prepared = media_path.with_name(media_path.stem + ("-ready.mp4" if is_mp4 else "-ready.mkv"))
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(media_path), "-map", "0:v:0",
    ]
    if selected_audio:
        command.extend(["-map", f"0:{selected_audio['index']}", "-disposition:a:0", "default"])
    else:
        command.append("-an")
    command.extend(["-map_metadata", "0", "-c", "copy"])
    if is_mp4:
        command.extend(["-movflags", "+faststart"])
        if video.get("codec_name") == "hevc":
            command.extend(["-tag:v", "hvc1"])
    command.append(str(prepared))
    result = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    if result.returncode:
        raise RuntimeError("Could not prepare the original video for playback without re-encoding.")
    if is_mp4:
        mark_normal_playback(prepared, video)
    if selected_audio:
        output_metadata, _ = probe_media(prepared)
        output_audio = next(stream for stream in output_metadata["streams"] if stream.get("codec_type") == "audio")
        if decode_audio(prepared, output_audio["index"]).returncode:
            raise RuntimeError("The prepared audio is unreadable; no file was sent.")
        if prepared != media_path and audio_fingerprint(media_path, selected_audio["index"]) != audio_fingerprint(prepared, output_audio["index"]):
            raise RuntimeError("The prepared audio differs from the original audio data.")
        print("Audio validated: clean source track preserved without re-encoding.")
    duration = validate_duration(prepared, source_duration)
    # Decode every video frame: metadata alone cannot establish that the full clip plays.
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin",
         "-i", str(prepared), "-map", "0:v:0", "-an", "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800, check=False,
    )
    if result.returncode:
        raise RuntimeError("The completed video contains unreadable frames; no download link was sent.")
    audio_codec = selected_audio.get("codec_name", "unknown") if selected_audio else "none"
    print(f"Playback validated: {duration:.1f}s; {prepared.suffix[1:]}, "
          f"{video.get('width', '?')}x{video.get('height', '?')}, "
          f"{video.get('codec_name', 'unknown')} video, {audio_codec} audio.")
    return prepared


def error_notice(error):
    if isinstance(error, DeliveryError):
        return str(error)
    detail = str(error)
    if isinstance(error, yt_dlp.utils.DownloadError):
        return "تعذر تنزيل المقطع من المصدر. تأكد إن الرابط متاح وجرّب مرة ثانية."
    if isinstance(error, ValueError):
        if detail.startswith(("ملف جلسة يوتيوب", "ملف الجلسة")):
            return detail
        return "رابط المقطع غير صالح أو غير مدعوم. تأكد من الرابط وحاول مرة ثانية."
    if detail.startswith(("Incomplete download", "The completed video contains unreadable", "The completed video has")):
        return "المقطع اللي وصل من المصدر ناقص أو فيه أجزاء ما تشتغل. ما أرسلناه حتى يصلك كامل."
    if detail.startswith(("The source audio", "The prepared audio", "Could not verify the original audio")):
        return "تعذر التحقق من سلامة الصوت الأصلي، فتوقف الإرسال."
    return "تعذر تجهيز المقطع أو إرساله. جرّب مرة ثانية بعد قليل."


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    raw_url = os.environ.get("VIDEO_URL", "").strip()
    youtube_session = os.environ.pop("YOUTUBE_COOKIES", "")
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets.", file=sys.stderr)
        return 2
    resources = ResourceClient()
    started = time.monotonic()
    try:
        if os.environ.get("VERIFY_R2_LINK") == "true":
            smoke_usage = resources.status()
            print(f"Resource ledger smoke status reliable: {smoke_usage.get('usage', {}).get('reliable') is True}.")
        url = validate_url(raw_url)
        is_youtube = urlparse(url).hostname in YOUTUBE_SESSION_HOSTS
        with tempfile.TemporaryDirectory(prefix="khalid-downloader-") as temp:
            template = str(Path(temp) / "%(title).100B-%(id)s.%(ext)s")
            guard = DownloadGuard(resources, temp)
            options = {
                "format": YOUTUBE_FORMAT if is_youtube else ORIGINAL_FORMAT,
                "outtmpl": template,
                "match_filter": guard.match,
                "progress_hooks": [guard.progress],
                "noplaylist": True,
                "restrictfilenames": True,
                "retries": 2,
                "fragment_retries": 2,
                "socket_timeout": 25,
                "js_runtimes": {"node": {}},
                "quiet": False,
            }
            info, media = download_source(url, options, guard, youtube_session)
            del youtube_session
            title = info.get("title", "video")
            if not media.is_file():
                raise RuntimeError("yt-dlp finished without its final merged video file.")
            print(f"Source download ready after {time.monotonic() - started:.2f}s.")
            guard.check(media.stat().st_size)
            media = prepare_delivery_media(media, info.get("duration"))
            print(f"Validated final file: {media.stat().st_size} bytes.")
            sending = time.monotonic()
            deliver(token, chat_id, media, title, resources)
            print(f"Delivery took {time.monotonic() - sending:.2f}s after media validation.")
            print("SUCCESS: original media delivered without transcoding.")
        return 0
    except Exception as error:
        # Upstream request exceptions can contain credentials. Only emit a safe error type.
        print(f"KHALID DOWNLOADER failed ({type(error).__name__}).", file=sys.stderr)
        if isinstance(error, ResourceBlocked):
            print(f"Resource protection reason: {error.reason}.", file=sys.stderr)
        try:
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": error_notice(error)})
        except DeliveryError:
            print("Telegram error notification was not confirmed.", file=sys.stderr)
        return 1
    finally:
        resources.notify(token, chat_id)


if __name__ == "__main__":
    raise SystemExit(main())
