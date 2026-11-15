# iPhone Shortcut setup (after Worker deployment)

Create a Shortcut named KHALID DOWNLOADER:

1. Add the action **Receive** and enable URLs (or use the Share Sheet input).
2. Add **Get URLs from Input** if the shared input is not already a URL.
3. Add **Get Contents of URL**. Set URL to your Cloudflare Worker endpoint and method to POST.
4. Add request headers: `Content-Type: application/json` and `Authorization: Bearer YOUR_API_SECRET`.
5. Set request body to JSON with key `url` and value the received URL.
6. Add **Show Notification** with the returned `message` field.

Keep the API secret private. Anyone who obtains it can queue jobs against your GitHub repository. Do not share the Shortcut publicly. This is a setup guide; the Shortcut cannot work until the Worker has been deployed and configured.
