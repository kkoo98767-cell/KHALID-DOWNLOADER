# Cloudflare Worker deployment

The GitHub Pages website origin is already configured in `wrangler.toml`:

`https://kkoo98767-cell.github.io`

## 1. Create the GitHub token

Create a fine-grained personal access token restricted to this repository:
- Repository access: Only select repositories > `KHALID-DOWNLOADER`
- Repository permissions: Actions = Read and write; Metadata = Read-only

Do not put this token in the website or iPhone Shortcut.

## 2. Deploy the Worker

Install Node.js on a computer, open a terminal in this directory, then run:

```bash
npx wrangler login
npx wrangler deploy
```

The Worker name is `khalid-downloader-api`. After deployment, copy the `workers.dev` URL shown by Wrangler.

## 3. Add Worker secrets

Run these commands and paste the requested values when prompted:

```bash
npx wrangler secret put API_SECRET
npx wrangler secret put GITHUB_TOKEN
```

Use a newly generated long random value for `API_SECRET` (at least 32 random bytes). Use the fine-grained GitHub token from step 1 for `GITHUB_TOKEN`.

## 4. Configure Cloudflare R2

Create a private R2 bucket for downloads. Create an R2 API token restricted to read and write access for that bucket. In GitHub repository Settings > Secrets and variables > Actions, add:
- `R2_ACCOUNT_ID`
- `R2_BUCKET_NAME`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`

Add a lifecycle rule to the bucket that deletes objects with the `khalid-downloads/` prefix after 7 days. The Telegram message contains a presigned download link that expires after 5 days. Files larger than Telegram's direct-upload size are stored unchanged; the video is not compressed or transcoded.

## 5. Connect the website

Open the deployed website, choose **Connection settings**, and enter:
- Worker URL: the exact `https://...workers.dev` URL printed by Wrangler
- API Secret: exactly the same value saved as the Worker secret `API_SECRET`

The allowed browser origin is set to `https://kkoo98767-cell.github.io` in `wrangler.toml`. If you change the website domain, update `ALLOWED_ORIGIN` and redeploy.

## 6. Verify Telegram delivery

In GitHub repository Settings > Secrets and variables > Actions, create:
- `TELEGRAM_BOT_TOKEN`: token from Telegram BotFather
- `TELEGRAM_CHAT_ID`: your destination chat ID

Start a conversation with your bot and send it `/start` before testing. Small files are sent directly to Telegram; large files arrive as a temporary download link.

Never share tokens or API secrets in chat, commit them to the repository, or place them in frontend code.
