# KHALID DOWNLOADER

Personal web + iPhone Shortcut workflow: submit a media URL, queue a GitHub Actions job, and receive eligible media in Telegram.

## Responsible use
Only download content you own or have permission to save. This project does not bypass DRM, paywalls, private-account restrictions, login controls, or other access restrictions. Follow copyright law and each platform's terms. Platform support depends on current public extractors; no universal availability is promised.

## Current architecture
1. Website / iPhone Shortcut submits a URL to a protected Cloudflare Worker.
2. Worker authenticates the request and dispatches this repository's workflow.
3. GitHub Actions runs yt-dlp for a single URL, without playlists or cookies.
4. Files up to 49 MiB are sent directly to Telegram. Larger files are uploaded unchanged to a private Cloudflare R2 bucket, and Telegram receives a temporary download link.

## Configure GitHub

In Settings > Secrets and variables > Actions, create:
- TELEGRAM_BOT_TOKEN: token created through Telegram BotFather.
- TELEGRAM_CHAT_ID: destination chat ID. Start the bot and send it a message first.
- R2_ACCOUNT_ID: Cloudflare account ID.
- R2_BUCKET_NAME: name of the private bucket `khalid-downloader-files`.
- R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY: R2 API credentials restricted to this bucket.

Create the R2 bucket and an R2 API token with read and write access restricted to it. Large files remain in R2 for about one hour: the Telegram link expires after one hour and the Cloudflare Worker removes expired objects every five minutes. Add an R2 lifecycle rule for the `khalid-downloads/` prefix to expire objects after one day as a cleanup backstop. The original media is not re-encoded or compressed.

## Cloudflare Worker
Deploy api/cloudflare-worker using Wrangler. Configure Worker secrets API_SECRET and GITHUB_TOKEN. Use a fine-grained GitHub token restricted to this repository with Actions read/write and metadata read-only. Set ALLOWED_ORIGIN to the exact HTTPS website origin. Never put the GitHub token or R2 credentials in frontend code or an iPhone Shortcut.

The Cloudflare Worker Git build is connected to this repository's `main` branch and uses `api/cloudflare-worker` as its root directory. Worker changes pushed to `main` deploy with `npx wrangler deploy`.

## Limitations
GitHub Actions is a short-lived runner, not a 24/7 server or permanent file host. Private, DRM-protected, login-required, or access-controlled media is unsupported. Public extractors may stop working when platforms change their sites. Do not expose an unrestricted public download endpoint.
