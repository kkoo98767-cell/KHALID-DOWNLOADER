import hashlib
import io
import json
import math
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import urlparse

import boto3
import requests
import yt_dlp
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_DIRECT_UPLOAD_LIMIT = 49 * 1024 * 1024
PRESIGNED_URL_EXPIRES_SECONDS = 60 * 60
R2_KEY_PREFIX = "khalid-downloads"
YOUTUBE_SESSION_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
YOUTUBE_COOKIE_DOMAINS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


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


def telegram_call(token, method, data=None, files=None):
    for attempt in range(3):
        if files:
            for _, stream in files.values():
                stream.seek(0)
        try:
            response = requests.post(
                f"{TELEGRAM_API}/bot{token}/{method}",
                data=data, files=files, timeout=(30, 900),
            )
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if attempt == 2 or (status is not None and status != 429 and status < 500):
                raise
            time.sleep(2 ** (attempt + 1))
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload


def notify(token, chat_id, message):
    telegram_call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": message[:3400] + "\n\n" + storage_usage_footer(),
    })


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
    print(f"Playback validated: {duration:.1f}s; original encoded video preserved.")
    return prepared


def file_sha256(media_path):
    with media_path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_download_link(link, expected_size, expected_hash):
    digest = hashlib.sha256()
    size = 0
    # Read the exact URL the recipient receives, without storing another copy or logging the URL.
    try:
        with requests.get(link, stream=True, timeout=(30, 900)) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                size += len(chunk)
                if size > expected_size:
                    raise RuntimeError("R2 download size differs from the original file.")
                digest.update(chunk)
    except requests.RequestException:
        raise RuntimeError("Could not verify the private R2 download link.") from None
    if size != expected_size or digest.hexdigest() != expected_hash:
        raise RuntimeError("R2 download does not match the original file; no link was sent.")
    print(f"R2 round-trip verified: {size} bytes; SHA-256 matches the original file.")


def r2_client_and_bucket(*, read_timeout=900, retries=5):
    account_id = os.environ.get("R2_ACCOUNT_ID", "").strip()
    bucket = os.environ.get("R2_BUCKET_NAME", "").strip()
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    missing = [
        name for name, value in {
            "R2_ACCOUNT_ID": account_id,
            "R2_BUCKET_NAME": bucket,
            "R2_ACCESS_KEY_ID": access_key,
            "R2_SECRET_ACCESS_KEY": secret_key,
        }.items() if not value
    ]
    if missing:
        raise RuntimeError("Cloudflare R2 settings are missing: " + ", ".join(missing))

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4", connect_timeout=15, read_timeout=read_timeout,
            retries={"max_attempts": retries},
            # Avoid SDK optional aws-chunked checksum trailers on S3-compatible storage.
            # Integrity is independently checked against the exact signed GET below.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    return s3, bucket


def format_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1000


def storage_usage_footer():
    try:
        s3, bucket = r2_client_and_bucket(read_timeout=20, retries=2)
        total_bytes = 0
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            total_bytes += sum(item["Size"] for item in page.get("Contents", []))
        value = format_size(total_bytes)
    except Exception:
        # An unavailable usage counter must not prevent delivery or pretend the bucket is empty.
        value = "تعذرت قراءتها الآن"
        print("R2 storage usage unavailable; delivering without a numeric total.")
    return "🗄 مساحة السيرفر (R2): " + value


DOWNLOAD_OPENERS = (
    "جاهز لك، استمتع بالمشاهدة 🎬",
    "تم التحميل، مقطعك صار جاهز ✨",
    "مقطعك وصل، مشاهدة ممتعة 🍿",
    "خلصنا التحميل، هذا مقطعك 🎞",
)


def download_message(title, media_size, footer, *, link=None, source=None):
    clean_title = re.sub(r"\s+", " ", str(title or "مقطع فيديو")).strip()[:150]
    lines = [
        secrets.choice(DOWNLOAD_OPENERS),
        "", clean_title,
        "🔊 الصورة والصوت بجودة المصدر، بدون إعادة ضغط.",
        "📦 حجم الملف: " + format_size(media_size),
    ]
    if link:
        lines.extend(["", "🔗 رابط التنزيل الخاص:", link, "", "⏳ الرابط صالح لمدة ساعة. احفظ المقطع قبل انتهاء الوقت."])
    elif source:
        lines.extend(["", "🔗 المصدر: " + source[:200]])
    lines.extend(["", "──────────────", footer])
    return "\n".join(lines)


def r2_download_link(media_path):
    s3, bucket = r2_client_and_bucket()
    safe_name = re.sub(r'[^A-Za-z0-9._-]', "_", media_path.name)[:180] or "video"
    key = f"{R2_KEY_PREFIX}/{secrets.token_hex(16)}/{safe_name}"
    content_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
    ascii_name = re.sub(r'[^A-Za-z0-9._-]', "_", media_path.name)[:180] or "video"
    encoded_name = quote(media_path.name, safe="")
    expected_size = media_path.stat().st_size
    expected_hash = file_sha256(media_path)
    for attempt in range(2):
        try:
            s3.upload_file(
                str(media_path), bucket, key,
                ExtraArgs={
                    "ContentType": content_type,
                    "ContentDisposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}",
                },
                Config=TransferConfig(
                    multipart_threshold=64 * 1024 * 1024,
                    multipart_chunksize=64 * 1024 * 1024,
                    max_concurrency=4, use_threads=True,
                ),
            )
            link = s3.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": key},
                ExpiresIn=PRESIGNED_URL_EXPIRES_SECONDS,
            )
            verify_download_link(link, expected_size, expected_hash)
            # Start the full one-hour validity after verification completes.
            return s3.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": key},
                ExpiresIn=PRESIGNED_URL_EXPIRES_SECONDS,
            )
        except Exception:
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except Exception:
                pass
            if attempt:
                raise RuntimeError("R2 upload verification failed twice; no download link was sent.") from None
            print("R2 verification failed; retrying upload once.")


def deliver(token, chat_id, media_path, source, title):
    media_size = media_path.stat().st_size
    if media_size <= TELEGRAM_DIRECT_UPLOAD_LIMIT:
        method = "sendVideo" if media_path.suffix.lower() in {".mp4", ".mov"} else "sendDocument"
        field = "video" if method == "sendVideo" else "document"
        caption = download_message(title, media_size, storage_usage_footer(), source=source)
        with media_path.open("rb") as stream:
            telegram_call(
                token,
                method,
                {"chat_id": chat_id, "caption": caption},
                {field: (media_path.name, stream)},
            )
        return

    link = r2_download_link(media_path)
    # Measure after upload, so this clip is included in the reported bucket total.
    message = download_message(title, media_size, storage_usage_footer(), link=link)
    telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": message})


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    raw_url = os.environ.get("VIDEO_URL", "").strip()
    youtube_session = os.environ.pop("YOUTUBE_COOKIES", "")
    cookies = None
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets.", file=sys.stderr)
        return 2
    try:
        url = validate_url(raw_url)
        download_url = url
        authentication = youtube_authentication(url, youtube_session)
        del youtube_session
        if authentication is not None:
            download_url, cookies = authentication
        with tempfile.TemporaryDirectory(prefix="khalid-downloader-") as temp:
            template = str(Path(temp) / "%(title).100B-%(id)s.%(ext)s")
            options = {
                "format": "bestvideo*+bestaudio/best",
                "outtmpl": template,
                "noplaylist": True,
                "restrictfilenames": True,
                "retries": 2,
                "fragment_retries": 2,
                "socket_timeout": 25,
                "js_runtimes": {"node": {}},
                "quiet": False,
            }
            completed_paths = []
            options["post_hooks"] = [completed_paths.append]
            if cookies is not None:
                options["cookiefile"] = cookies
                options["allowed_extractors"] = ["youtube"]
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(download_url, download=True)
                    title = info.get("title", "video")
                    final_path = completed_paths[-1] if completed_paths else info.get("filepath")
                    if not final_path:
                        final_path = ydl.prepare_filename(info)
            finally:
                if cookies is not None:
                    cookies.close()
            media = Path(final_path)
            if not media.is_file():
                raise RuntimeError("yt-dlp finished without its final merged video file.")
            media = prepare_delivery_media(media, info.get("duration"))
            print(f"Validated final file: {media.stat().st_size} bytes.")
            deliver(token, chat_id, media, url, title)
            print("SUCCESS: original media delivered without transcoding.")
        return 0
    except Exception as exc:
        message = "KHALID DOWNLOADER failed: " + type(exc).__name__ + ": " + str(exc)[:800]
        print(message, file=sys.stderr)
        notice = message
        if isinstance(exc, yt_dlp.utils.DownloadError):
            detail = str(exc).casefold().replace("’", "'")
            if "[youtube]" in detail and "sign in to confirm" in detail:
                notice = (
                    "يوتيوب طلب تسجيل الدخول للتحقق قبل تنزيل هذا المقطع. "
                    + ("جلسة الحساب لم تُقبل من السيرفر." if cookies is not None
                       else "لم تُضف جلسة يوتيوب إلى السيرفر بعد.")
                )
        try:
            notify(token, chat_id, notice)
        except Exception as notify_error:
            print("Telegram error notification failed: " + str(notify_error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
