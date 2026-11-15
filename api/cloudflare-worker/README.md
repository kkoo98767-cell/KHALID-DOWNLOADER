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

## 4. Connect the website

Open the deployed website, choose **Connection settings**, and enter:
- Worker URL: the exact `https://...workers.dev` URL printed by Wrangler
- API Secret: exactly the same value saved as the Worker secret `API_SECRET`

The allowed browser origin is set to `https://kkoo98767-cell.github.io` in `wrangler.toml`. If you change the website domain, update `ALLOWED_ORIGIN` and redeploy.

## 5. Verify Telegram delivery

In GitHub repository Settings > Secrets and variables > Actions, create:
- `TELEGRAM_BOT_TOKEN`: token from Telegram BotFather
- `TELEGRAM_CHAT_ID`: your destination chat ID

Start a conversation with your bot and send it `/start` before testing. Then submit a public video URL that you own or are authorized to save. The workflow currently limits Telegram uploads to 49 MiB.

Never share tokens or API secrets in chat, commit them to the repository, or place them in frontend code.
