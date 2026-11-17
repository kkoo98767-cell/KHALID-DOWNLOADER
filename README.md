# KHALID DOWNLOADER

Personal web + iPhone Shortcut workflow: submit a media URL, queue a GitHub Actions job, and receive eligible media in Telegram.

## Responsible use
Only download content you own or have permission to save. This project does not bypass DRM, paywalls, private-account restrictions, login controls, or other access restrictions. Follow copyright law and each platform's terms. Platform support depends on current public extractors; no universal availability is promised.

## Current architecture
1. Website / iPhone Shortcut submits a URL to a protected Cloudflare Worker.
2. Worker authenticates the request and dispatches this repository's workflow.
3. GitHub Actions runs yt-dlp for a single URL without playlists. An optional YouTube session is used only for HTTPS YouTube video links. The runner verifies the complete video and copies the selected encoded tracks without encoding again.
4. Files up to 50 MB go directly from the runner to the hosted Telegram Bot API when supported. The Worker does not transfer or decode their bytes.
5. A SQLite Durable Object reserves R2 capacity for large files. The runner uploads directly to R2 with per-request checksums, then the Worker issues a one-hour capability URL. The Worker controls and counts each real R2 read and streams the requested bytes. A Durable Object alarm queues deletion at expiry.

## Configure GitHub

In Settings > Secrets and variables > Actions, create:
- TELEGRAM_BOT_TOKEN: token created through Telegram BotFather.
- TELEGRAM_CHAT_ID: destination chat ID. Start the bot and send it a message first.
- R2_ACCOUNT_ID: Cloudflare account ID.
- R2_BUCKET_NAME: name of the private bucket `khalid-downloader-files`.
- R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY: R2 API credentials restricted to this bucket.

Use a private, dedicated R2 Standard bucket with credentials scoped to that bucket. Keep its public development and custom-domain access disabled so reads use the guarded URL. Add R2 lifecycle rules for the `khalid-downloads/` prefix to expire completed objects after one day and abort incomplete multipart uploads after one day as a provider-side backstop. The one-hour expiry is enforced immediately by the Worker on new download requests; physical deletion is retried after a temporary storage error.

An owner who chooses to enable YouTube authentication can add the Netscape-formatted session for their YouTube-only account as the Actions secret `YOUTUBE_COOKIES`. Paste it directly into GitHub Secrets. Keep it out of the repository, logs, and messages. This is an account credential and may expire or be rejected by YouTube. Delete the secret to turn off authenticated requests. See [yt-dlp session guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).

## Cloudflare Worker
Deploy api/cloudflare-worker using Wrangler. Configure Worker secrets API_SECRET and GITHUB_TOKEN. Use a fine-grained GitHub token restricted to this repository with Actions read/write and metadata read-only. Set ALLOWED_ORIGIN to the exact HTTPS website origin. Never put the GitHub token or R2 credentials in frontend code or an iPhone Shortcut.

The Cloudflare Worker Git build is connected to this repository's `main` branch and uses `api/cloudflare-worker` as its root directory. Worker changes pushed to `main` deploy with `npx wrangler deploy`.

The downloader workflow uses the `RESOURCE_API_URL` environment setting and a short-lived, audience-bound GitHub Actions identity. The Worker validates the signature, repository ID, workflow file, event and branch. The browser's API secret only authorizes queue requests. No resource account key belongs in the browser.

## Resource budgets and recovery

The configured operating ceilings are 9.9 GB of active project storage, 990,000 Class A attempts per month, and 9,900,000 Class B reads per month. Set `R2_STORAGE_LIMIT`, `R2_CLASS_A_LIMIT`, `R2_CLASS_B_LIMIT` or `R2_MAX_OBJECT_BYTES` in `wrangler.toml` to allocate less to this project. An upload reserves exactly its file size, a bounded number of calls based on its upload shape and retries, and one initial Class B read. Concurrent reservations use a single persistent transaction. Deletion releases storage; historical request usage remains counted.

The initial inventory takes one Class A operation per list page. Thereafter, registered objects and reservations provide the storage estimate without periodic bucket listings. Set `R2_CLASS_A_BASELINE` and `R2_CLASS_B_BASELINE` before inventory to include known earlier operations. The conservative rolling usage window includes the entire oldest UTC day that could overlap 31 days. This works with billing periods that start on different days. Counters cover the project's operations. Use Cloudflare billing and R2 analytics for account totals. Cloudflare Standard storage is billed in GB-month using daily peaks.

Existing presigned links issued before cutover still follow their original one-hour expiration. On the first authenticated resource check, the ledger reconciles the dedicated bucket, including eligible legacy files. A request rejected by a resource budget does not disable normal direct Telegram delivery. Downloads that have already started streaming can finish after expiry. New requests must present an unexpired link and have read capacity.

Arabic warnings and recovery notices normally travel with the downloader job. When a protection state changes in the background, the Worker queues one GitHub Actions notification for that batch. Limits, reads, and cleanup do not wait for Telegram.

Deploy the Worker and confirm that `/health` returns `resourceApi: 1` and `resourceLedger: true` before enabling the new downloader workflow. If a backend regression requires rollback, revert the downloader and keep the ledger Worker deployed until current guarded links expire. Preserve the SQLite migration and bindings while those links remain live.

## Limitations
GitHub Actions is a short-lived runner, not a 24/7 server or permanent file host. Private, DRM-protected, login-required, or access-controlled media is unsupported. Public extractors may stop working when platforms change their sites. Do not expose an unrestricted public download endpoint.
