import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";
import { dispatchLease, newDispatchLease } from "../src/dispatch.js";
import { authorizedResourceRequest } from "../src/oidc.js";
import { sendBackgroundNotices } from "../src/resource-notices.js";

const issuer = "https://token.actions.githubusercontent.com";
const now = Math.floor(Date.now() / 1000);
const env = { GITHUB_OWNER: "example", GITHUB_REPO: "downloader", GITHUB_REPO_ID: "1234" };
const claims = {
  iss: issuer, aud: "khalid-downloader-resource-api", repository: "example/downloader",
  repository_id: "1234", workflow_ref: "example/downloader/.github/workflows/downloader.yml@refs/heads/main",
  ref: "refs/heads/main", event_name: "workflow_dispatch", exp: now + 300, nbf: now - 5
};

test("OIDC accepts a signed main runner identity and rejects wrong scope or tampering", async t => {
  const keys = await crypto.subtle.generateKey({
    name: "RSASSA-PKCS1-v1_5", hash: "SHA-256", modulusLength: 2048,
    publicExponent: new Uint8Array([1, 0, 1])
  }, true, ["sign", "verify"]);
  const jwk = await crypto.subtle.exportKey("jwk", keys.publicKey);
  jwk.kid = "trusted";
  const remote = t.mock.method(globalThis, "fetch", async address => {
    assert.equal(address, issuer + "/.well-known/jwks");
    return Response.json({ keys: [jwk] });
  });
  const encode = input => Buffer.from(JSON.stringify(input)).toString("base64url");
  const header = encode({ alg: "RS256", kid: jwk.kid });
  const toRequest = token => new Request("https://test.invalid/internal/resources", {
    headers: { Authorization: "Bearer " + token }
  });
  async function sign(payload) {
    const input = header + "." + encode(payload);
    const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", keys.privateKey, new TextEncoder().encode(input));
    return input + "." + Buffer.from(signature).toString("base64url");
  }
  const token = await sign(claims);
  assert.equal(await authorizedResourceRequest(toRequest(token), env), true);
  assert.equal(await authorizedResourceRequest(toRequest(token), env), true);
  assert.equal(remote.mock.callCount(), 1);
  for (const change of [
    { repository_id: "9999" }, { aud: "elsewhere" }, { event_name: "pull_request" },
    { workflow_ref: "example/downloader/.github/workflows/other.yml@refs/heads/main" },
    { ref: "refs/heads/topic" }, { exp: now - 100 }, { nbf: now + 100 },
  ]) {
    assert.equal(await authorizedResourceRequest(toRequest(await sign({ ...claims, ...change })), env), false);
  }
  const [h, p, signature] = token.split(".");
  const altered = (signature[0] === "A" ? "B" : "A") + signature.slice(1);
  assert.equal(await authorizedResourceRequest(toRequest([h, p, altered].join(".")), env), false);
  assert.equal(await authorizedResourceRequest(toRequest("frontend-secret"), env), false);
});

test("SQLite dispatch lease deduplicates URL variants and releases only the matching lease", async () => {
  const db = new DatabaseSync(":memory:");
  const storage = {
    sql: { exec(query, ...params) {
      const rows = db.prepare(query).all(...params);
      return { toArray: () => rows };
    } },
    transactionSync: fn => fn()
  };
  const first = await newDispatchLease(new URL("https://youtu.be/VFE3slVdK3g?si=example"));
  const next = await newDispatchLease(new URL("https://www.youtube.com/watch?v=VFE3slVdK3g"));
  assert.equal(first.id, next.id);
  assert.equal(dispatchLease(storage, "/dispatch/lease", first).duplicate, false);
  assert.equal(dispatchLease(storage, "/dispatch/lease", next).duplicate, true);
  dispatchLease(storage, "/dispatch/release", next);
  assert.equal(dispatchLease(storage, "/dispatch/lease", next).duplicate, true);
  dispatchLease(storage, "/dispatch/release", first);
  assert.equal(dispatchLease(storage, "/dispatch/lease", next).duplicate, false);
  db.close();
});

test("background alerts retain a rejected batch and clear it once GitHub accepts", async t => {
  const db = new DatabaseSync(":memory:");
  const storage = {
    sql: { exec(query, ...params) {
      const rows = db.prepare(query).all(...params);
      return { toArray: () => rows };
    } },
    transactionSync: fn => fn()
  };
  db.exec("CREATE TABLE resource_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)");
  db.prepare("INSERT INTO resource_meta VALUES (?,?)").run("notices", JSON.stringify(["رجعت عمليات التحميل."]));
  let status = 503;
  const request = t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(new URL(url).pathname, "/repos/example/downloader/actions/workflows/resource-notice.yml/dispatches");
    assert.deepEqual(JSON.parse(options.body), { ref: "main", inputs: { notice: "رجعت عمليات التحميل." } });
    return new Response(null, { status });
  });
  const settings = { ...env, RESOURCE_NOTICE_WORKFLOW: "resource-notice.yml", GITHUB_TOKEN: "test-only" };
  assert.equal(await sendBackgroundNotices(settings, storage), false);
  status = 204;
  assert.equal(await sendBackgroundNotices(settings, storage), true);
  assert.equal(await sendBackgroundNotices(settings, storage), true);
  assert.equal(request.mock.callCount(), 2);
  db.close();
});
