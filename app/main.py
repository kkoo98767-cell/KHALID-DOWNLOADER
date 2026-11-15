import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp

STANDARD_API_LIMIT_BYTES = 49 * 1024 * 1024
LOCAL_API_LIMIT_BYTES = 1990 * 1024 * 1024
TELEGRAM_API = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/")
LOCAL_API_ENABLED = TELEGRAM_API.startswith("http://127.0.0.1:8081")


def validate_url(raw):
    value = (raw or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("A valid HTTP(S) URL is required.")
    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith((".local", ".internal")):
        raise ValueError("Local network URLs are not accepted.")
    if re.search(r"[\\x00-\\x20]", value):
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


def deliver(token, chat_id, media_path, source):
    file_size = media_path.stat().st_size
    if LOCAL_API_ENABLED and file_size > LOCAL_API_LIMIT_BYTES:
        raise RuntimeError("This file exceeds the local Telegram Bot API upload limit of 1990 MiB.")
    if not LOCAL_API_ENABLED and file_size > STANDARD_API_LIMIT_BYTES:
        raise RuntimeError(
            "This file is larger than Telegram's standard Bot API limit. "
            "Configure TELEGRAM_API_ID and TELEGRAM_API_HASH in GitHub Actions to enable lossless uploads up to 1990 MiB."
        )

    method = "sendVideo" if media_path.suffix.lower() in {".mp4", ".mov"} else "sendDocument"
    field = "video" if method == "sendVideo" else "document"
    with media_path.open("rb") as stream:
        telegram_call(
            token,
            method,
            {"chat_id": chat_id, "caption": "KHALID DOWNLOADER | Source: " + source[:700]},
            {field: (media_path.name, stream)},
        )


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
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "video")
            candidates = [p for p in Path(temp).iterdir() if p.is_file()]
            if not candidates:
                raise RuntimeError("No media file was produced.")
            media = max(candidates, key=lambda p: p.stat().st_size)
            deliver(token, chat_id, media, url)
            print("SUCCESS: delivered original file without transcoding: " + title)
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
