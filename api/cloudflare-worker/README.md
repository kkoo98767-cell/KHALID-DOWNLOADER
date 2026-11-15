# Cloudflare Worker deployment

From this directory, deploy with Wrangler: `npx wrangler deploy`.

Set secrets: `npx wrangler secret put API_SECRET` and `npx wrangler secret put GITHUB_TOKEN`.

Edit wrangler.toml and set ALLOWED_ORIGIN to the exact HTTPS origin of the deployed website, then redeploy. Use a fine-grained GitHub token restricted to this repository with Actions read/write permission and Metadata read-only. Keep secrets out of browser code and the iPhone Shortcut.
