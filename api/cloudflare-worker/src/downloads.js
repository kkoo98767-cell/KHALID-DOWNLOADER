const extensions = { mp4: "video/mp4", mov: "video/quicktime", mkv: "video/x-matroska", webm: "video/webm" };

export const DOWNLOAD_PREFIX = "khalid-downloads/";
export const DOWNLOAD_RETENTION_MS = 60 * 60 * 1000;
export const DOWNLOAD_TOKEN = /^[a-f0-9]{48}$/;

export function byteRange(value, size) {
  if (!value) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(value);
  if (!match || (!match[1] && !match[2])) return false;
  const [start, end] = match.slice(1).map(Number);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end)) return false;
  if (!match[1]) {
    if (end === 0) return false;
    return { offset: Math.max(0, size - end), length: Math.min(size, end) };
  }
  const last = match[2] ? Math.min(end, size - 1) : size - 1;
  if (start >= size || last < start) return false;
  return { offset: start, length: last - start + 1 };
}

export function publicDownloadOptions(request, token) {
  return {
    token,
    method: request.method,
    range: request.headers.get("Range")?.slice(0, 256),
    ifRange: request.headers.get("If-Range")?.slice(0, 256),
    ifNoneMatch: request.headers.get("If-None-Match")?.slice(0, 512),
  };
}

export function parseReadRequest(input, record) {
  const tag = `"${record.etag}"`;
  const nonMatch = input.ifNoneMatch?.split(",").map(value => value.trim().replace(/^W\//, ""));
  if (nonMatch?.some(value => value === "*" || value === tag)) return { status: 304 };
  if (input.method === "HEAD") return { status: 200, head: true };
  const range = byteRange(!input.ifRange || input.ifRange === tag ? input.range : null, record.size);
  if (range === false) return { status: 416 };
  return { status: range ? 206 : 200, range };
}

export async function streamDownload(instruction, bucket) {
  const { status, key, size, etag, range, head } = instruction;
  const headers = new Headers({
    "cache-control": "private, no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "accept-ranges": "bytes",
  });
  if (status === 429) headers.set("Retry-After", "60");
  if (status === 416) headers.set("Content-Range", `bytes */${size}`);
  if (etag) headers.set("ETag", `"${etag}"`);
  if (status !== 200 && status !== 206) return new Response(null, { status, headers });

  const file = key.split("/").pop();
  headers.set("content-type", extensions[file.split(".").pop().toLowerCase()] || "application/octet-stream");
  const asciiFile = file.replace(/[^A-Za-z0-9_.-]/g, "_");
  headers.set("content-disposition", `attachment; filename="${asciiFile}"; filename*=UTF-8''${encodeURIComponent(file).replace(/['()*]/g, c => `%${c.charCodeAt(0).toString(16)}`)}`);
  headers.set("x-object-size", `${size}`);
  headers.set("content-length", `${range?.length ?? size}`);
  if (range) headers.set("content-range", `bytes ${range.offset}-${range.offset + range.length - 1}/${size}`);
  if (head) return new Response(null, { status, headers });

  // The worker streams a single R2 operation for a GET, including a Range GET.
  try {
    const object = await bucket.get(key, { onlyIf: { etagMatches: etag }, ...(range ? { range } : {}) });
    if (!object?.body || object.size !== size) {
      await object?.body?.cancel();
      headers.delete("content-length");
      headers.delete("content-range");
      return new Response(null, { status: 410, headers });
    }
    const output = new FixedLengthStream(range?.length ?? size);
    // A pipe error reaches the client through the readable side as well.
    object.body.pipeTo(output.writable).catch(() => {});
    return new Response(output.readable, { status, headers });
  } catch {
    return new Response(null, { status: 503, headers: { "cache-control": "no-store", "retry-after": "30" } });
  }
}
