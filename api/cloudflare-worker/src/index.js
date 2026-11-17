import { authorizedResourceRequest } from "./oidc.js";
import { DOWNLOAD_TOKEN, publicDownloadOptions, streamDownload } from "./downloads.js";
import { newDispatchLease } from "./dispatch.js";

export { ResourceLedger } from "./resource-ledger.js";

const VERSION = "2026-11-17.1";

const reply = (body, status = 200, headers = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...headers
    }
  });

const RESOURCE_PATHS = new Map([
  ["/internal/resources", ["GET", "/status"]],
  ["/internal/resources/reserve", ["POST", "/reserve"]],
  ["/internal/resources/complete", ["POST", "/complete"]],
  ["/internal/resources/cancel", ["POST", "/cancel"]],
  ["/internal/resources/upload-id", ["POST", "/upload-id"]],
]);

function ledger(env) {
  return env.RESOURCE_LEDGER?.getByName("downloads");
}

async function ledgerDispatch(stub, path, lease) {
  const response = await stub.fetch("https://ledger.internal/dispatch/" + path, {
    method: "POST",
    body: JSON.stringify(lease),
  });
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error("Dispatch ledger unavailable");
  return result;
}

async function smallBody(request) {
  const reader = request.body?.getReader();
  if (!reader) return "";
  const decoder = new TextDecoder();
  let size = 0;
  let body = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) return body + decoder.decode();
    size += value.byteLength;
    if (size > 8192) {
      await reader.cancel();
      return null;
    }
    body += decoder.decode(value, { stream: true });
  }
}

async function protectedResource(request, env, spec) {
  const [method, path] = spec;
  if (request.method !== method) return reply({ ok: false, reason: "method" }, 405);
  if (!await authorizedResourceRequest(request, env)) return reply({ ok: false, reason: "unauthorized" }, 401);
  const stub = ledger(env);
  if (!stub || !env.DOWNLOADS) return reply({ ok: false, reason: "unavailable", usage: { reliable: false } }, 503);
  try {
    const body = method === "POST" ? await smallBody(request) : undefined;
    if (body === null) return reply({ ok: false, reason: "invalid" }, 413);
    const result = await stub.fetch(`https://ledger.internal${path}`, { method, body });
    const payload = await result.json();
    if (payload.token) {
      const { token, ...safe } = payload;
      const link = new URL(`/download/${token}`, request.url);
      link.protocol = "https:";
      return reply({ ...safe, url: link.href }, result.status);
    }
    return reply(payload, result.status);
  } catch {
    console.warn("Resource ledger unavailable");
    return reply({ ok: false, reason: "unavailable", usage: { reliable: false } }, 503);
  }
}

async function protectedDownload(request, env, token) {
  if (!DOWNLOAD_TOKEN.test(token)) return new Response(null, { status: 404 });
  if (!["GET", "HEAD"].includes(request.method)) return new Response(null, { status: 405, headers: { allow: "GET, HEAD" } });
  const stub = ledger(env);
  if (!stub || !env.DOWNLOADS) return new Response(null, { status: 503 });
  try {
    const response = await stub.fetch("https://ledger.internal/read", {
      method: "POST",
      body: JSON.stringify(publicDownloadOptions(request, token)),
    });
    return streamDownload(await response.json(), env.DOWNLOADS);
  } catch {
    return new Response(null, { status: 503, headers: { "cache-control": "no-store" } });
  }
}

export default {
  async scheduled(_event, env, ctx) {
    const stub = ledger(env);
    if (stub) ctx.waitUntil(stub.fetch("https://ledger.internal/legacy-clean", { method: "POST", body: "{}" }));
  },

  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname.startsWith("/download/")) return protectedDownload(request, env, pathname.slice("/download/".length));
    if (RESOURCE_PATHS.has(pathname)) return protectedResource(request, env, RESOURCE_PATHS.get(pathname));
    if (pathname !== "/" && pathname !== "/health") return reply({ ok: false, error: "Not found" }, 404);

    const origin = request.headers.get("Origin") || "";
    const allowedOrigin = env.ALLOWED_ORIGIN || "";
    const originAllowed = Boolean(origin && allowedOrigin && origin === allowedOrigin);
    const cors = originAllowed
      ? {
          "access-control-allow-origin": origin,
          "vary": "Origin"
        }
      : {};

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
        version: VERSION,
        resourceApi: 1,
        resourceLedger: Boolean(env.RESOURCE_LEDGER && env.DOWNLOADS)
      };
      if (request.headers.get("Authorization") === `Bearer ${env.API_SECRET || ""}` && env.API_SECRET) {
        info.missing = ["API_SECRET", "GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO", "ALLOWED_ORIGIN"]
          .filter(key => !env[key]);
      }
      return reply(info, 200, cors);
    }

    if (request.method !== "POST" || pathname !== "/") {
      return reply({ ok: false, error: "Method not allowed" }, 405, cors);
    }

    if (!env.API_SECRET || request.headers.get("Authorization") !== `Bearer ${env.API_SECRET}`) {
      return reply({ ok: false, error: "Unauthorized" }, 401, cors);
    }

    let body;
    try {
      const rawBody = await smallBody(request);
      if (rawBody === null) return reply({ ok: false, error: "Request too large" }, 413, cors);
      body = JSON.parse(rawBody);
    } catch {
      return reply({ ok: false, error: "Invalid JSON" }, 400, cors);
    }

    const raw = typeof body?.url === "string" ? body.url.trim() : "";
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

    const stub = ledger(env);
    let lease;
    if (stub) {
      try {
        lease = await newDispatchLease(parsed);
        if ((await ledgerDispatch(stub, "lease", lease)).duplicate) {
          return reply({ ok: true, duplicate: true, message: "This video was requested recently." }, 202, cors);
        }
      } catch {
        console.warn("Dispatch deduplication temporarily unavailable");
      }
    }
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
        body: JSON.stringify({ ref: "main", inputs: { url: raw } }),
        signal: AbortSignal.timeout(10000)
      });
    } catch {
      console.error("GitHub dispatch response unavailable");
      return reply({ ok: false, error: "Worker could not reach GitHub" }, 502, cors);
    }

    if (![200, 204].includes(gh.status)) {
      if (lease && gh.status >= 400 && gh.status < 500) {
        try { await ledgerDispatch(stub, "release", lease); }
        catch { console.warn("Dispatch lease will expire automatically"); }
      }
      console.error("GitHub workflow dispatch rejected", { status: gh.status });
      return reply({
        ok: false,
        error: `GitHub could not queue the job (HTTP ${gh.status})`
      }, 502, cors);
    }

    return reply({
      ok: true,
      message: "Download job queued. Telegram will receive the result when complete."
    }, 202, cors);
  }
};
