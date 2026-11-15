# KHALID DOWNLOADER

Personal web + iPhone Shortcut workflow: submit a media URL, queue a GitHub Actions job, and receive eligible media in Telegram.

## Responsible use
Only download content you own or have permission to save. This project does not bypass DRM, paywalls, private-account restrictions, login controls, or other access restrictions. Follow copyright law and each platform's terms. Platform support depends on current public extractors; no universal availability is promised.

## Current architecture
1. Website / iPhone Shortcut submits a URL to a protected Cloudflare Worker.
2. Worker authenticates the request and dispatches this repository's workflow.
3. GitHub Actions runs yt-dlp for a single URL, without playlists or cookies.
4. The resulting file is sent to Telegram; runner temporary files are removed when the job ends.

## Configure GitHub
In Settings > Secrets and variables > Actions, create:
- TELEGRAM_BOT_TOKEN: token created through Telegram BotFather.
- TELEGRAM_CHAT_ID: destination chat ID. Start the bot and send it a message first.

Files larger than 49 MiB are automatically compressed to fit Telegram's bot upload limit. The compressed video is encoded as MP4 (H.264/AAC); shorter videos retain more quality than longer ones. If the video cannot fit at a usable quality, the workflow sends a clear error message instead.

## Cloudflare Worker
Deploy api/cloudflare-worker using Wrangler. Configure Worker secrets API_SECRET and GITHUB_TOKEN. Use a fine-grained GitHub token restricted to this repository with Actions read/write and metadata read-only. Set ALLOWED_ORIGIN to the exact HTTPS website origin. Never put the GitHub token in frontend code or an iPhone Shortcut.

## Limitations
GitHub Actions is a short-lived runner, not a 24/7 server or permanent file host. Private, DRM-protected, login-required, or access-controlled media is unsupported. Public extractors may stop working when platforms change their sites. Do not expose an unrestricted public download endpoint.
