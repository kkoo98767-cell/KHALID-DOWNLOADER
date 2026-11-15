# Cloudflare Worker deployment

The GitHub Pages website origin is already configured in `wrangler.toml`:

`https://kkoo98767-cell.github.io`

## 1. Create the GitHub token

Create a fine-grained personal access token restricted to this repository:
- Repository access: Only select repositories > `KHALID-DOWNLOADER`
- Repository permissions: Actions = Read and write; Metadata = Read-only

Do not put this token in the website or iPhone Shortcut.

## 2. Configure Cloudflare R2

Create a private R2 bucket named `khalid-downloader-files`. Create an R2 API token restricted to read and write access for this bucket. In GitHub repository Settings > Secrets and variables > Actions, add:
- `R2_ACCOUNT_ID`
- `R2_BUCKET_NAME` with value `khalid-downloader-files`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`

The Worker uses an R2 bucket binding to remove objects older than one hour, checking every five minutes. The Telegram link expires after one hour. Add a one-day R2 lifecycle expiry for prefix `khalid-downloads/` as a backstop:
```bash
npx wrangler r2 bucket lifecycle add khalid-downloader-files khalid-downloads/ --expire-days 1
```

## 3. Deploy the Worker

Install Node.js on a computer, open a terminal in this directory, then run:

```bash
npx wrangler login
npx wrangler deploy
```

The Worker name is `khalid-downloader-api`.

## 4. Add Worker secrets

Run these commands and paste the requested values when prompted:

```bash
npx wrangler secret put API_SECRET
npx wrangler secret put GITHUB_TOKEN
```

Use a newly generated long random value for `API_SECRET` (at least 32 random bytes). Use a fine-grained GitHub token restricted to this repository with Actions read/write and metadata read-only for `GITHUB_TOKEN`. Never put either value in the website or an iPhone Shortcut.

## 5. Connect the website

Open the deployed website, choose **Connection settings**, and enter:
- Worker URL: the exact `https://...workers.dev` URL printed by Wrangler
- API Secret: exactly the same value saved as the Worker secret `API_SECRET`

The allowed browser origin is set to `https://kkoo98767-cell.github.io` in `wrangler.toml`. If you change the website domain, update `ALLOWED_ORIGIN` and redeploy.

## 6. Verify Telegram delivery

In GitHub repository Settings > Secrets and variables > Actions, create:
- `TELEGRAM_BOT_TOKEN`: token from Telegram BotFather
- `TELEGRAM_CHAT_ID`: your destination chat ID

Start a conversation with your bot and send it `/start`. Small files are sent directly to Telegram; large files arrive as one-hour download links.

Never share tokens or API secrets in chat, commit them to the repository, or place them in frontend code.
