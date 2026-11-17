# Cloudflare Worker

`POST /` accepts the configured browser/Shortcut API secret and queues the repository's downloader workflow. The Worker applies a two-minute duplicate-request window for matching URLs and accepts preflight from the configured Pages origin. The Worker does not proxy uploads to Telegram or R2.

`GET /health` reports the release version and the presence of the resource bindings. The runner authenticates with a short-lived GitHub Actions OIDC token for audience `khalid-downloader-resource-api`. The Worker checks the configured immutable repository ID, the downloader workflow path, `workflow_dispatch`, and the main branch. The browser API credential does not grant access to the internal endpoints.

The `RESOURCE_LEDGER` SQLite Durable Object manages upload reservations, rolling R2 usage, private download tokens, and deletion alarms. An R2 download link supports `HEAD`, conditional GET, and single byte ranges. The Worker streams from the private `DOWNLOADS` binding. It counts one Class B operation whenever it actually asks R2 for data. It serves metadata from the recorded upload.

Configure `API_SECRET` and `GITHUB_TOKEN` as Worker secrets. Give the GitHub token repository Actions write and metadata read permissions. Keep R2 credentials and the Telegram bot token in the downloader's GitHub Actions secrets.

The Git-based deployment uses this directory as the build root and runs `npx wrangler deploy`. On a new account, deploy the SQLite migration and the R2 binding before enabling the resource protocol in the runner. Keep the R2 bucket private and use Standard storage. A one-day lifecycle for completed objects and abandoned multipart uploads under `khalid-downloads/` provides a backstop for the one-hour alarm.

The budget settings and rollback procedure are in the [project README](../../README.md#resource-budgets-and-recovery). To verify the streaming path on a real large job, run the downloader manually with the optional `verify_r2_link` input. It reads one byte and does not log the download URL.
