const reply = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json; charset=utf-8", ...headers } });

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const cors = origin && env.ALLOWED_ORIGIN && origin === env.ALLOWED_ORIGIN ? { "access-control-allow-origin": origin, "vary": "Origin" } : {};
    if (request.method === "OPTIONS") {
      if (!cors["access-control-allow-origin"]) return new Response(null, { status: 403 });
      return new Response(null, { status: 204, headers: { ...cors, "access-control-allow-methods": "POST, OPTIONS", "access-control-allow-headers": "content-type, authorization", "access-control-max-age": "86400" } });
    }
    if (request.method !== "POST") return reply({ ok: false, error: "Method not allowed" }, 405, cors);
    if (!env.API_SECRET || request.headers.get("Authorization") !== `Bearer ${env.API_SECRET}`) return reply({ ok: false, error: "Unauthorized" }, 401, cors);
    let body;
    try { body = await request.json(); } catch { return reply({ ok: false, error: "Invalid JSON" }, 400, cors); }
    const raw = typeof body.url === "string" ? body.url.trim() : "";
    let parsed;
    try { parsed = new URL(raw); } catch { return reply({ ok: false, error: "A valid URL is required" }, 400, cors); }
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || raw.length > 2000) return reply({ ok: false, error: "Only ordinary HTTP(S) URLs up to 2000 characters are accepted" }, 400, cors);
    if (!env.GITHUB_TOKEN || !env.GITHUB_OWNER || !env.GITHUB_REPO) return reply({ ok: false, error: "Worker is not configured" }, 503, cors);
    const workflow = env.GITHUB_WORKFLOW || "downloader.yml";
    const endpoint = `https://api.github.com/repos/${encodeURIComponent(env.GITHUB_OWNER)}/${encodeURIComponent(env.GITHUB_REPO)}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`;
    const gh = await fetch(endpoint, { method: "POST", headers: { accept: "application/vnd.github+json", authorization: `Bearer ${env.GITHUB_TOKEN}`, "x-github-api-version": "2022-11-28", "user-agent": "khalid-downloader-worker", "content-type": "application/json" }, body: JSON.stringify({ ref: "main", inputs: { url: raw } }) });
    if (gh.status !== 204) return reply({ ok: false, error: "GitHub could not queue the job", detail: (await gh.text()).slice(0, 400) }, 502, cors);
    return reply({ ok: true, message: "Download job queued. Telegram will receive the result when complete." }, 202, cors);
  }
};
