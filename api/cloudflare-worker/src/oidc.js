const ISSUER = "https://token.actions.githubusercontent.com";
const AUDIENCE = "khalid-downloader-resource-api";
const SIGNATURE = { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" };
const KEY_TTL_MS = 5 * 60 * 1000;
let keyCache;

function decodeBase64Url(segment) {
  const binary = atob(segment.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(binary, char => char.charCodeAt(0));
}

function readJson(segment) {
  return JSON.parse(new TextDecoder().decode(decodeBase64Url(segment)));
}

async function loadKeys() {
  const response = await fetch(ISSUER + "/.well-known/jwks", {
    redirect: "manual",
    signal: AbortSignal.timeout(5000),
    cf: { cacheEverything: true, cacheTtlByStatus: { "200": 300, "400-599": 0 } }
  });
  if (!response.ok) throw new Error("GitHub signing keys unavailable");
  const { keys } = await response.json();
  const imported = await Promise.all(keys
    .filter(key => key.kty === "RSA" && (!key.alg || key.alg === "RS256") &&
      (!key.use || key.use === "sig") && typeof key.kid === "string")
    .map(async key => [key.kid, await crypto.subtle.importKey("jwk", key, SIGNATURE, false, ["verify"])]));
  return new Map(imported);
}

async function signingKeys() {
  if (keyCache?.until > Date.now()) return keyCache.keys;
  const keys = await loadKeys();
  keyCache = { until: Date.now() + KEY_TTL_MS, keys };
  return keys;
}

export async function authorizedResourceRequest(request, env, onReject = () => {}) {
  const reject = reason => { onReject(reason); return false; };
  const authorization = request.headers.get("Authorization") || "";
  if (!env.GITHUB_REPO_ID || !env.GITHUB_OWNER || !env.GITHUB_REPO) return reject("configuration");
  const match = /^Bearer ([\w-]+)\.([\w-]+)\.([\w-]+)$/.exec(authorization);
  if (!match || authorization.length > 8192) return reject("token-format");
  const [, headerSegment, claimsSegment, signatureSegment] = match;
  try {
    const header = readJson(headerSegment);
    const claims = readJson(claimsSegment);
    const repository = env.GITHUB_OWNER + "/" + env.GITHUB_REPO;
    const workflow = env.GITHUB_WORKFLOW || "downloader.yml";
    const now = Math.floor(Date.now() / 1000);
    if (header.alg !== "RS256" || typeof header.kid !== "string" || header.crit) return reject("token-format");
    if (claims.iss !== ISSUER || claims.aud !== AUDIENCE) return reject("issuer");
    if (claims.repository !== repository || String(claims.repository_id) !== String(env.GITHUB_REPO_ID) ||
        claims.workflow_ref !== repository + "/.github/workflows/" + workflow + "@refs/heads/main" ||
        claims.ref !== "refs/heads/main" || claims.event_name !== "workflow_dispatch") return reject("workflow");
    if (!Number.isSafeInteger(claims.exp) || claims.exp <= now ||
        !Number.isSafeInteger(claims.nbf) || claims.nbf > now + 15) return reject("time");
    const key = (await signingKeys()).get(header.kid);
    if (!key) return reject("signing-key");
    return await crypto.subtle.verify(
      SIGNATURE, key, decodeBase64Url(signatureSegment),
      new TextEncoder().encode(headerSegment + "." + claimsSegment)
    ) || reject("signature");
  } catch {
    return reject("verify");
  }
}
