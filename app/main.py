import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp

MAX_UPLOAD_BYTES = 49 * 1024 * 1024
TARGET_UPLOAD_BYTES = 46 * 1024 * 1024
TELEGRAM_API = "https://api.telegram.org"


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
    response = requests.post(f"{TELEGRAM_API}/bot{token}/{method}", data=data, files=files, timeout=(30, 180))
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload


def notify(token, chat_id, message):
    telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": message[:3900]})


def compress_video(media_path, duration, temp_dir):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("This video is too large for Telegram and ffmpeg is not available to compress it.")
    if not duration or duration <= 0:
        raise RuntimeError("This video is too large for Telegram and its duration could not be determined.")

    # Leave a few MiB of headroom below Telegram's upload cap for reliable multipart uploads.
    total_kbps = (TARGET_UPLOAD_BYTES * 8) / duration / 1000
    audio_kbps = 96
    video_kbps = math.floor(total_kbps - audio_kbps)
    if video_kbps < 150:
        raise RuntimeError("This video is too long to fit Telegram's upload limit at a usable quality.")

    compressed = Path(temp_dir) / "telegram-compressed.mp4"
    for attempt in range(3):
        rate = max(150, math.floor(video_kbps * (0.85 ** attempt)))
        command = [
            "ffmpeg", "-y", "-i", str(media_path),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{rate}k",
            "-maxrate", f"{rate}k", "-bufsize", f"{rate * 2}k",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart", str(compressed),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Video compression exceeded the 15-minute limit.") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "ffmpeg failed")[-1200:]
            raise RuntimeError("Could not compress the video: " + detail) from exc
        if compressed.exists() and compressed.stat().st_size <= MAX_UPLOAD_BYTES:
            return compressed

    raise RuntimeError("The video could not be compressed below Telegram's 49 MiB upload limit.")


def deliver(token, chat_id, media_path, source):
    method = "sendVideo" if media_path.suffix.lower() in {".mp4", ".mov"} else "sendDocument"
    field = "video" if method == "sendVideo" else "document"
    with media_path.open("rb") as stream:
        telegram_call(token, method, {"chat_id": chat_id, "caption": "KHALID DOWNLOADER | Source: " + source[:700]}, {field: (media_path.name, stream)})


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
            options = {"format": "bestvideo*+bestaudio/best", "outtmpl": template, "noplaylist": True, "restrictfilenames": True, "retries": 2, "fragment_retries": 2, "socket_timeout": 25, "quiet": False}
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "video")
                duration = info.get("duration")
            candidates = [p for p in Path(temp).iterdir() if p.is_file()]
            if not candidates:
                raise RuntimeError("No media file was produced.")
            media = max(candidates, key=lambda p: p.stat().st_size)
            if media.stat().st_size > MAX_UPLOAD_BYTES:
                if not info.get("vcodec") and not any(p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"} for p in candidates):
                    raise RuntimeError("Downloaded media exceeds Telegram's 49 MiB limit and cannot be compressed as a video.")
                media = compress_video(media, duration, temp)
            deliver(token, chat_id, media, url)
            print("SUCCESS: delivered " + title)
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
