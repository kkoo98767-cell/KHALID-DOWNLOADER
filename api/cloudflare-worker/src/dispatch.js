const DUPLICATE_WINDOW_MS = 120_000;
const YOUTUBE = new Set(["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"]);

function dispatchIdentity(url) {
  let videoId;
  if (url.hostname === "youtu.be") videoId = url.pathname.slice(1).replace(/\/$/, "");
  if (YOUTUBE.has(url.hostname)) {
    videoId = url.pathname === "/watch"
      ? url.searchParams.get("v")
      : /^\/(?:shorts|live|embed)\/([\w-]{11})\/?$/.exec(url.pathname)?.[1];
  }
  return /^[\w-]{11}$/.test(videoId || "") ? "youtube:" + videoId : url.href;
}

export async function newDispatchLease(url) {
  const bytes = new TextEncoder().encode(dispatchIdentity(url));
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  const id = Array.from(digest, b => b.toString(16).padStart(2, "0")).join("");
  return { id, lease: crypto.randomUUID() };
}

export function dispatchLease(storage, path, { id, lease }) {
  if (!/^[0-9a-f]{64}$/.test(id || "") || !/^[0-9a-f-]{36}$/.test(lease || "")) {
    return { ok: false };
  }
  const { sql } = storage;
  sql.exec(`CREATE TABLE IF NOT EXISTS dispatch_leases (
    id TEXT PRIMARY KEY, lease TEXT NOT NULL, expires INTEGER NOT NULL
  )`);
  sql.exec("CREATE INDEX IF NOT EXISTS dispatch_expiry ON dispatch_leases(expires)");
  if (path === "/dispatch/release") {
    sql.exec("DELETE FROM dispatch_leases WHERE id = ? AND lease = ?", id, lease);
    return { ok: true };
  }
  const now = Date.now();
  return storage.transactionSync(() => {
    sql.exec(`DELETE FROM dispatch_leases WHERE id IN (
      SELECT id FROM dispatch_leases WHERE expires <= ? LIMIT 100
    )`, now);
    const [existing] = sql.exec("SELECT expires FROM dispatch_leases WHERE id = ?", id).toArray();
    if (existing && existing.expires > now) return { ok: true, duplicate: true };
    sql.exec("INSERT OR REPLACE INTO dispatch_leases VALUES (?, ?, ?)", id, lease, now + DUPLICATE_WINDOW_MS);
    return { ok: true, duplicate: false };
  });
}
