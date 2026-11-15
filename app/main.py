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


def probe_duration(media_path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
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
        duration = float(result.stdout.strip())
    except (TypeError, ValueError):
        raise RuntimeError("Could not read the completed video's duration.") from None
    if duration <= 0:
        raise RuntimeError("The completed video has no playable duration.")
    return duration


def validate_duration(media_path, source_duration):
    actual = probe_duration(media_path)
    try:
        expected = float(source_duration)
    except (TypeError, ValueError):
        expected = 0
    if expected > 2:
        tolerance = max(2.0, expected * 0.15)
        if actual + tolerance < expected:
            raise RuntimeError(
                f"Incomplete download detected: source is about {expected:.1f}s, "
                f"but the final file is only {actual:.1f}s."
            )
    return actual


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
        config=Config(signature_version="s3v4", connect_timeout=30, read_timeout=900, retries={"max_attempts": 5}),
    )
    safe_name = re.sub(r'[^A-Za-z0-9._-]', "_", media_path.name)[:180] or "video"
    key = f"{R2_KEY_PREFIX}/{secrets.token_hex(16)}/{safe_name}"
    content_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
    ascii_name = re.sub(r'[^A-Za-z0-9._-]', "_", media_path.name)[:180] or "video"
    encoded_name = quote(media_path.name, safe="")
    s3.upload_file(
        str(media_path),
        bucket,
        key,
        ExtraArgs={
            "ContentType": content_type,
            "ContentDisposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}",
        },
        Config=TransferConfig(
            multipart_threshold=64 * 1024 * 1024,
            multipart_chunksize=64 * 1024 * 1024,
            max_concurrency=4,
            use_threads=True,
        ),
    )
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=PRESIGNED_URL_EXPIRES_SECONDS,
    )


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
            duration = validate_duration(media, info.get("duration"))
            print(f"Validated final video: {duration:.1f} seconds, {media.stat().st_size} bytes.")
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
