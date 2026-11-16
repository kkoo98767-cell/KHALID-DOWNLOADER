import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
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


def telegram_call(token, method, data=None, files=None):
    response = requests.post(
        f"{TELEGRAM_API}/bot{token}/{method}",
        data=data,
        files=files,
        timeout=(30, 900),
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload


def notify(token, chat_id, message):
    telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": message[:3900]})


def probe_media(media_path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,codec_name,duration,width,height,nb_frames",
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


def prepare_delivery_media(media_path, source_duration):
    validate_duration(media_path, source_duration)
    if media_path.suffix.lower() not in {".mp4", ".mov", ".m4v"}:
        prepared = media_path
    else:
        _, video = probe_media(media_path)
        prepared = media_path.with_name(media_path.stem + "-ready.mp4")
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(media_path), "-map", "0:v:0", "-map", "0:a?",
            "-map_metadata", "0", "-c", "copy", "-movflags", "+faststart",
            "-avoid_negative_ts", "make_zero",
        ]
        if video.get("codec_name") == "hevc":
            command.extend(["-tag:v", "hvc1"])
        command.append(str(prepared))
        result = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
        if result.returncode:
            raise RuntimeError("Could not prepare the original video for playback without re-encoding.")
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


def r2_download_link(media_path):
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
        raise RuntimeError("Large files need Cloudflare R2. Missing Actions secrets: " + ", ".join(missing))

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4", connect_timeout=30, read_timeout=900,
            retries={"max_attempts": 5},
            # Avoid SDK optional aws-chunked checksum trailers on S3-compatible storage.
            # Integrity is independently checked against the exact signed GET below.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
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
    if media_path.stat().st_size <= TELEGRAM_DIRECT_UPLOAD_LIMIT:
        method = "sendVideo" if media_path.suffix.lower() in {".mp4", ".mov"} else "sendDocument"
        field = "video" if method == "sendVideo" else "document"
        with media_path.open("rb") as stream:
            telegram_call(
                token,
                method,
                {"chat_id": chat_id, "caption": "KHALID DOWNLOADER | Source: " + source[:700]},
                {field: (media_path.name, stream)},
            )
        return

    link = r2_download_link(media_path)
    message = (
        "الفيديو الأصلي جاهز بدون ضغط أو تغيير في الجودة.\n"
        f"{title[:300]}\n\n"
        "رابط تنزيل خاص، صالح لمدة ساعة واحدة:\n"
        f"{link}"
    )
    telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": message})


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    raw_url = os.environ.get("VIDEO_URL", "").strip()
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets.", file=sys.stderr)
        return 2
    try:
        url = validate_url(raw_url)
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
                "quiet": False,
            }
            completed_paths = []
            options["post_hooks"] = [completed_paths.append]
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "video")
                final_path = completed_paths[-1] if completed_paths else info.get("filepath")
                if not final_path:
                    final_path = ydl.prepare_filename(info)
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
        try:
            notify(token, chat_id, message)
        except Exception as notify_error:
            print("Telegram error notification failed: " + str(notify_error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
