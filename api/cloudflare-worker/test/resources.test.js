import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";
import { byteRange, streamDownload } from "../src/downloads.js";

registerHooks({
  resolve(specifier, context, next) {
    if (specifier === "cloudflare:workers") {
      return { url: "data:text/javascript,export class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }", shortCircuit: true };
    }
    return next(specifier, context);
  },
});

const { ResourceLedger } = await import("../src/resource-ledger.js");
const ID1 = "77777777-7777-4777-8777-777777777777";
const ID2 = "99999999-9999-4999-8999-999999999999";
const KEY1 = `khalid-downloads/${ID1}/clip.mp4`;
const KEY2 = `khalid-downloads/${ID2}/clip.mp4`;
const ETAG = "0123456789abcdef0123456789abcdef";

function harness(config = {}, initialObjects = []) {
  const db = new DatabaseSync(":memory:");
  const bucket = {
    lists: 0,
    removed: [],
    aborted: [],
    async list() { this.lists++; return { objects: initialObjects, truncated: false }; },
    async delete(keys) { this.removed.push(...keys); },
    resumeMultipartUpload(key, id) {
      return { abort: async () => { bucket.aborted.push({ key, id }); } };
    },
  };
  const storage = {
    next: null,
    sql: {
      exec(query, ...bindings) {
        let result = [];
        if (query.startsWith("\n")) db.exec(query);
        else {
          const statement = db.prepare(query);
          if (statement.columns().length) result = statement.all(...bindings);
          else statement.run(...bindings);
        }
        return { toArray: () => result, one: () => { assert.equal(result.length, 1); return result[0]; } };
      },
    },
    transactionSync(run) {
      db.exec("BEGIN");
      try { const result = run(); db.exec("COMMIT"); return result; }
      catch (error) { db.exec("ROLLBACK"); throw error; }
    },
    async getAlarm() { return this.next; },
    async setAlarm(value) { this.next = value; },
  };
  const env = { DOWNLOADS: bucket, R2_STORAGE_LIMIT: "1000", R2_CLASS_A_LIMIT: "10", R2_CLASS_B_LIMIT: "10", ...config };
  let ledger = new ResourceLedger({ storage }, env);
  return {
    bucket, storage,
    restart() { ledger = new ResourceLedger({ storage }, env); },
    async alarm() { storage.next = null; await ledger.alarm(); },
    async call(route, input) {
      const request = new Request(`https://ledger.internal${route}`, input ? { method: "POST", body: JSON.stringify(input) } : {});
      const response = await ledger.fetch(request);
      return { status: response.status, data: await response.json() };
    },
  };
}

test("persistent reservations cap concurrent storage and recover after alarm cleanup", async t => {
  let now = Date.UTC(2026, 10, 17, 12);
  t.mock.method(Date, "now", () => now);
  const legacy = { key: "khalid-downloads/old.mp4", size: 700, uploaded: new Date(now - 100) };
  const other = { key: "do-not-delete.mp4", size: 10, uploaded: new Date(now - 3_700_000) };
  const h = harness({}, [legacy, other]);
  const attempts = await Promise.all([
    h.call("/reserve", { id: ID1, key: KEY1, size: 200, classA: 2 }),
    h.call("/reserve", { id: ID2, key: KEY2, size: 200, classA: 2 }),
  ]);
  const winner = attempts[0].status === 200 ? { id: ID1, key: KEY1 } : { id: ID2, key: KEY2 };
  assert.deepEqual(attempts.map(r => r.status).sort(), [200, 429]);
  assert.equal(h.bucket.lists, 1);
  assert.equal(attempts.find(r => r.status === 429).data.reason, "storage");
  assert.equal(h.storage.next, legacy.uploaded.getTime() + 3_600_000);

  h.restart();
  const manifest = { id: winner.id, usedClassA: 1, verifiedSize: 200, etag: ETAG };
  const first = await h.call("/complete", manifest);
  const again = await h.call("/complete", manifest);
  assert.equal(first.data.token, again.data.token);
  assert.equal(first.data.expiresAt, now + 3_600_000);
  assert.equal(again.data.usage.classA.used, 2);
  assert.equal(again.data.usage.storage.used, 910);

  now += 3_600_000;
  assert.equal((await h.call("/read", { token: first.data.token, method: "GET" })).data.status, 410);
  await h.alarm();
  const after = (await h.call("/status")).data;
  assert.equal(after.usage.storage.used, 10);
  assert.equal(after.usage.classA.used, 2);
  assert.match(after.notices.join(" "), /رجعنا نشغّل/);
  assert.deepEqual(h.bucket.removed.sort(), [winner.key, legacy.key].sort());
  assert.equal(h.bucket.lists, 1);
});

test("HEAD, validators and invalid ranges use no R2 reads; each actual GET takes one allowance", async () => {
  const h = harness({ R2_CLASS_B_LIMIT: "2" });
  await h.call("/reserve", { id: ID1, key: KEY1, size: 100, classA: 1 });
  await h.call("/reserve", { id: ID2, key: KEY2, size: 100, classA: 1 });
  const manifest = { usedClassA: 1, verifiedSize: 100, etag: ETAG };
  const a = (await h.call("/complete", { id: ID1, ...manifest })).data.token;
  const b = (await h.call("/complete", { id: ID2, ...manifest })).data.token;
  assert.equal((await h.call("/read", { token: a, method: "HEAD" })).data.head, true);
  assert.equal((await h.call("/read", { token: a, method: "GET", ifNoneMatch: `W/"${ETAG}"` })).data.status, 304);
  assert.equal((await h.call("/read", { token: a, method: "GET", range: "bytes=100-" })).data.status, 416);
  assert.equal((await h.call("/status")).data.usage.classB.used, 0);
  const partial = (await h.call("/read", { token: a, method: "GET", range: "bytes=17-38" })).data;
  assert.equal(partial.status, 206);
  assert.deepEqual(partial.range, { offset: 17, length: 22 });
  assert.equal((await h.call("/read", { token: a, method: "GET" })).data.status, 429);
  assert.equal((await h.call("/read", { token: b, method: "GET" })).data.status, 200);
  assert.equal((await h.call("/status")).data.usage.classB.used, 2);
});

test("multipart reservations release unused ops and only release storage after successful deletion", async t => {
  let now = Date.UTC(2026, 10, 17, 12);
  t.mock.method(Date, "now", () => now);
  const h = harness();
  const reserve = await h.call("/reserve", { id: ID1, key: KEY1, size: 600, classA: 6 });
  assert.equal(reserve.data.usage.classA.reserved, 6);
  await h.call("/upload-id", { id: ID1, uploadId: "fake-upload-id" });
  const cancel = { id: ID1, usedClassA: 4 };
  assert.equal((await h.call("/cancel", cancel)).data.usage.classA.used, 5);
  assert.equal((await h.call("/cancel", cancel)).data.usage.classA.used, 5);
  const deletion = t.mock.method(h.bucket, "delete", () => { throw new Error("failed delete"); });
  await h.alarm();
  assert.equal((await h.call("/status")).data.usage.storage.used, 600);
  assert.deepEqual(h.bucket.aborted, [{ key: KEY1, id: "fake-upload-id" }]);
  deletion.mock.restore();
  now += 300_000;
  await h.alarm();
  assert.equal((await h.call("/status")).data.usage.storage.used, 0);
});

test("rolling allowance stays capped through UTC reset, charges vanished jobs once and lets whole days expire", async t => {
  let now = Date.UTC(2026, 10, 30, 23, 45);
  t.mock.method(Date, "now", () => now);
  const h = harness({ R2_CLASS_A_LIMIT: "5", R2_CLASS_A_BASELINE: "2" });
  await h.call("/reserve", { id: ID1, key: KEY1, size: 600, classA: 2 });
  assert.equal((await h.call("/status")).data.usage.classA.used, 3);
  const blocked = await h.call("/reserve", { id: ID2, key: KEY2, size: 100, classA: 1 });
  assert.equal(blocked.data.reason, "classA");
  now += 3_900_001;
  const next = (await h.call("/status")).data;
  assert.equal(next.usage.classA.used, 3);
  assert.equal(next.usage.classA.reserved, 2);
  assert.equal(next.blockingReason, "classA");
  assert.doesNotMatch(next.notices.join(" "), /رجعنا نشغّل/);
  await h.alarm();
  assert.equal((await h.call("/status")).data.usage.classA.used, 5);
  assert.equal((await h.call("/status")).data.usage.storage.used, 0);
  assert.equal(h.storage.next, Date.UTC(2027, 0, 1));
  now = Date.UTC(2026, 11, 31, 23, 59, 59);
  assert.equal((await h.call("/status")).data.usage.classA.used, 5);
  now = Date.UTC(2027, 0, 1);
  await h.alarm();
  const recovered = (await h.call("/status")).data;
  assert.equal(recovered.usage.classA.used, 2);
  assert.match(recovered.notices.join(" "), /رجعنا نشغّل/);
});

test("valid byte-range stream requests one ETag-matched read without buffering", async t => {
  const previous = globalThis.FixedLengthStream;
  globalThis.FixedLengthStream = class extends TransformStream {
    constructor() { super(); }
  };
  t.after(() => {
    if (previous === undefined) delete globalThis.FixedLengthStream;
    else globalThis.FixedLengthStream = previous;
  });
  const requests = [];
  const payload = new Uint8Array([4, 5, 6]);
  const response = await streamDownload({ status: 206, key: KEY1, size: 10, etag: ETAG, range: byteRange("bytes=-3", 10) }, {
    async get(key, options) {
      requests.push({ key, options });
      return { size: 10, body: new Blob([payload]).stream() };
    },
  });
  assert.equal(response.status, 206);
  assert.equal(response.headers.get("content-range"), "bytes 7-9/10");
  assert.equal(response.headers.get("content-length"), "3");
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), payload);
  assert.deepEqual(requests, [{ key: KEY1, options: { onlyIf: { etagMatches: ETAG }, range: { offset: 7, length: 3 } } }]);
});
