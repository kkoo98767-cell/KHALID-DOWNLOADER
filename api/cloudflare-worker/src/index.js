const VERSION = "2026-11-15.3";

const reply = (body, status = 200, headers = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...headers
    }
  });

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const allowedOrigin = env.ALLOWED_ORIGIN || "";
    const originAllowed = Boolean(origin && allowedOrigin && origin === allowedOrigin);
    const cors = originAllowed
      ? {
          "access-control-allow-origin": origin,
          "vary": "Origin"
        }
      : {};

    // Log only diagnostic metadata; never log API secrets, GitHub tokens, or submitted URLs.
    console.log("Request received", {
      method: request.method,
      origin: origin || "(none)",
      allowedOriginConfigured: Boolean(allowedOrigin),
      originAllowed,
      requestedMethod: request.headers.get("Access-Control-Request-Method") || "",
      requestedHeaders: request.headers.get("Access-Control-Request-Headers") || ""
    });

    if (request.method === "OPTIONS") {
      if (!originAllowed) {
        console.warn("CORS preflight rejected: Origin does not exactly match ALLOWED_ORIGIN");
        return new Response(null, { status: 403 });
      }
      return new Response(null, {
        status: 204,
        headers: {
          ...cors,
          "access-control-allow-methods": "POST, OPTIONS",
          "access-control-allow-headers": "content-type, authorization",
          "access-control-max-age": "86400"
        }
      });
    }

    if (request.method === "GET") {
      const info = {
        ok: true,
        service: "khalid-downloader-api",
        version: VERSION
      };
      if (request.headers.get("Authorization") === `Bearer ${env.API_SECRET || ""}` && env.API_SECRET) {
        info.missing = ["API_SECRET", "GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO", "ALLOWED_ORIGIN"]
          .filter(key => !env[key]);
      }
      return reply(info, 200, cors);
    }

    if (request.method !== "POST") {
      return reply({ ok: false, error: "Method not allowed" }, 405, cors);
    }

    if (!env.API_SECRET || request.headers.get("Authorization") !== `Bearer ${env.API_SECRET}`) {
      return reply({ ok: false, error: "Unauthorized" }, 401, cors);
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return reply({ ok: false, error: "Invalid JSON" }, 400, cors);
    }

    const raw = typeof body.url === "string" ? body.url.trim() : "";
    let parsed;
    try {
      parsed = new URL(raw);
    } catch {
      return reply({ ok: false, error: "A valid URL is required" }, 400, cors);
    }

    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || raw.length > 2000) {
      return reply({ ok: false, error: "Only ordinary HTTP(S) URLs up to 2000 characters are accepted" }, 400, cors);
    }

    if (!env.GITHUB_TOKEN || !env.GITHUB_OWNER || !env.GITHUB_REPO) {
      return reply({ ok: false, error: "Worker is not configured" }, 503, cors);
    }

    const workflow = env.GITHUB_WORKFLOW || "downloader.yml";
    const endpoint = `https://api.github.com/repos/${encodeURIComponent(env.GITHUB_OWNER)}/${encodeURIComponent(env.GITHUB_REPO)}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`;

    let gh;
    try {
      gh = await fetch(endpoint, {
        method: "POST",
        headers: {
          accept: "application/vnd.github+json",
          authorization: `Bearer ${env.GITHUB_TOKEN}`,
          "x-github-api-version": "2022-11-28",
          "user-agent": "khalid-downloader-worker",
          "content-type": "application/json"
        },
        body: JSON.stringify({ ref: "main", inputs: { url: raw } })
      });
    } catch (error) {
      console.error("GitHub request failed", String(error));
      return reply({ ok: false, error: "Worker could not reach GitHub" }, 502, cors);
    }

    if (gh.status !== 204) {
      const text = (await gh.text()).slice(0, 600);
      let detail = text;
      try {
        detail = JSON.parse(text).message || text;
      } catch {}
      console.error("GitHub workflow dispatch rejected", { status: gh.status, detail: String(detail).slice(0, 300) });
      return reply({
        ok: false,
        error: `GitHub could not queue the job (HTTP ${gh.status})`,
        detail: String(detail).slice(0, 300)
      }, 502, cors);
    }

    return reply({
      ok: true,
      message: "Download job queued. Telegram will receive the result when complete."
    }, 202, cors);
  }
};
