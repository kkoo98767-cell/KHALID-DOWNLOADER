import { DurableObject } from "cloudflare:workers";
import { dispatchLease } from "./dispatch.js";
import { sendBackgroundNotices } from "./resource-notices.js";
import {
  DOWNLOAD_PREFIX, DOWNLOAD_RETENTION_MS, DOWNLOAD_TOKEN, parseReadRequest,
} from "./downloads.js";

const MINUTE = 60_000;
const DAY = 24 * 60 * MINUTE;
const BILLING_GUARD_DAYS = 31;
const RESERVATION_TTL = 65 * MINUTE;
const EXPIRE_BATCH = 20;
const MULTIPART_THRESHOLD = 128 * 1024 * 1024;
const PART_BYTES = 64 * 1024 * 1024;
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const ETAG = /^[a-f0-9]{32}(?:-[1-9]\d{0,4})?$/i;
const RESOURCE_NAMES = {
  storage: "مساحة التخزين المؤقتة",
  classA: "عمليات التخزين من الفئة أ",
  classB: "عمليات القراءة من الفئة ب",
};

const reply = (data, status = 200) => Response.json(data, { status });
const utcDay = ms => Math.floor(ms / DAY);
// Include the whole oldest UTC day because the provider cycle can start at any instant.
const oldestDay = ms => utcDay(ms) - BILLING_GUARD_DAYS;

function integer(value, min, max) {
  return Number.isSafeInteger(value) && value >= min && value <= max;
}

function config(env, key, fallback, max) {
  const value = Number(env[key] ?? fallback);
  if (!integer(value, 0, max)) throw new Error(`Invalid ${key} setting`);
  return value;
}

function quota(env, name, max) {
  const allowance = config(env, `R2_${name}_ALLOWANCE`, max, max);
  const limit = config(env, `R2_${name}_LIMIT`, allowance, allowance);
  return { allowance, limit };
}

function token() {
  const random = crypto.getRandomValues(new Uint8Array(24));
  return Array.from(random, byte => byte.toString(16).padStart(2, "0")).join("");
}

function validKey(key) {
  return typeof key === "string" && key.length <= 512 && key.startsWith(DOWNLOAD_PREFIX)
    && key.slice(DOWNLOAD_PREFIX.length).split("/")
      .every(part => /^[A-Za-z0-9_.-]+$/.test(part) && part !== "." && part !== "..");
}

function level(resource) {
  const ratio = resource.allowance ? (resource.used + resource.reserved) / resource.allowance : 1;
  return ratio >= .95 ? 3 : ratio >= .85 ? 2 : ratio >= .70 ? 1 : 0;
}

function missingUpload(error) {
  return error?.code === "NoSuchUpload"
    || /NoSuchUpload|not exist|not found|already (aborted|completed)/i.test(error?.message || "");
}

export class ResourceLedger extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.limits = {
      storage: quota(env, "STORAGE", 10_000_000_000),
      classA: quota(env, "CLASS_A", 1_000_000),
      classB: quota(env, "CLASS_B", 10_000_000),
    };
    this.maxObject = config(env, "R2_MAX_OBJECT_BYTES", this.limits.storage.limit, 10_000_000_000);
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS resource_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS resource_day (day INTEGER PRIMARY KEY, used_a INTEGER NOT NULL, used_b INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS resource_files (
        id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, size INTEGER NOT NULL, state TEXT NOT NULL,
        expires INTEGER, pending_a INTEGER NOT NULL DEFAULT 0,
        pending_b INTEGER NOT NULL DEFAULT 0, token TEXT UNIQUE, etag TEXT, upload_id TEXT
      );
      CREATE INDEX IF NOT EXISTS resource_expiry ON resource_files (expires);
      CREATE INDEX IF NOT EXISTS resource_pending_read ON resource_files (pending_b) WHERE pending_b = 1;
    `);
  }

  object(id) {
    return this.sql.exec("SELECT * FROM resource_files WHERE id = ?", id).toArray()[0];
  }

  meta(name, fallback) {
    const row = this.sql.exec("SELECT value FROM resource_meta WHERE name = ?", name).toArray()[0];
    return row ? JSON.parse(row.value) : fallback;
  }

  setMeta(name, value) {
    this.sql.exec("INSERT OR REPLACE INTO resource_meta VALUES (?, ?)", name, JSON.stringify(value));
  }

  charge(now, classA, classB) {
    if (!classA && !classB) return;
    this.sql.exec(`INSERT INTO resource_day VALUES (?, ?, ?) ON CONFLICT(day) DO UPDATE
      SET used_a = used_a + excluded.used_a, used_b = used_b + excluded.used_b`, utcDay(now), classA, classB);
  }

  usageCounts(now) {
    return this.sql.exec(`SELECT COALESCE(SUM(used_a),0) AS used_a, COALESCE(SUM(used_b),0) AS used_b
      FROM resource_day WHERE day>=?`, oldestDay(now)).one();
  }

  current(now) {
    const counts = this.usageCounts(now);
    const totals = this.sql.exec(`SELECT
      COALESCE(SUM(CASE WHEN state != 'pending' THEN size ELSE 0 END), 0) AS stored,
      COALESCE(SUM(CASE WHEN state = 'pending' THEN size ELSE 0 END), 0) AS reserved,
      COALESCE(SUM(pending_a), 0) AS pending_a,
      COALESCE(SUM(pending_b), 0) AS pending_b
      FROM resource_files`).one();
    return {
      storage: { used: totals.stored, reserved: totals.reserved, ...this.limits.storage },
      classA: { used: counts.used_a, reserved: totals.pending_a, ...this.limits.classA, windowDays: BILLING_GUARD_DAYS },
      classB: { used: counts.used_b, reserved: totals.pending_b, ...this.limits.classB, windowDays: BILLING_GUARD_DAYS },
      reliable: this.meta("initialized", false),
      scope: "project-estimate",
    };
  }

  free(resource) {
    return Math.max(0, resource.limit - resource.used - resource.reserved);
  }

  readUsage(now) {
    const counts = this.sql.exec(`SELECT
      COALESCE((SELECT SUM(used_b) FROM resource_day WHERE day>=?), 0) AS used,
      (SELECT COUNT(*) FROM resource_files WHERE pending_b=1) AS reserved`, oldestDay(now)).one();
    return { ...counts, ...this.limits.classB };
  }

  chargeReservedRead(now) {
    const { limit } = this.limits.classB;
    if (!limit) return false;
    return this.sql.exec(`INSERT INTO resource_day SELECT ?, 0, 1
      WHERE COALESCE((SELECT SUM(used_b) FROM resource_day WHERE day>=?),0)<?
      ON CONFLICT(day) DO UPDATE SET used_b=used_b+1 RETURNING used_b`,
    utcDay(now), oldestDay(now), limit).toArray().length > 0;
  }

  transitions(usage, blocked = null, demand = 0, drain = true) {
    const previous = this.meta("levels", {});
    const stopped = this.meta("blocked", {});
    const notices = this.meta("notices", []);
    const levels = {};
    let stoppedChanged = false;
    let levelsChanged = false;
    for (const [name, label] of Object.entries(RESOURCE_NAMES)) {
      const severity = level(usage[name]);
      levels[name] = severity;
      levelsChanged ||= severity !== previous[name];
      if (severity > (previous[name] ?? 0)) {
        notices.push(`تنبيه: ${label} ${severity === 3 ? "وصلت لمستوى خطر" : severity === 2 ? "اقتربت من الحد" : "ارتفع استخدامها"}.`);
      }
      if (name === blocked && !stopped[name]) {
        stopped[name] = demand;
        stoppedChanged = true;
        notices.push(`وقفنا تلقائيًا العمليات اللي تحتاج ${label} مؤقتًا لأن المتاح ما يكفي.`);
      } else if (name !== blocked && stopped[name] && this.free(usage[name]) >= stopped[name]) {
        delete stopped[name];
        stoppedChanged = true;
        notices.push(`رجعنا نشغّل العمليات الخاصة بـ${label} بعد ما تأكدنا إن فيه موارد تكفي.`);
      }
    }
    if (levelsChanged) this.setMeta("levels", levels);
    if (stoppedChanged) this.setMeta("blocked", stopped);
    if (drain ? notices.length > 0 : notices.length !== this.meta("notices", []).length) {
      this.setMeta("notices", drain ? [] : notices.slice(-12));
    }
    return notices;
  }

  snapshot(now, blocked, demand) {
    const usage = this.current(now);
    const freeA = this.free(usage.classA);
    const freeB = this.free(usage.classB);
    const freeStorage = this.free(usage.storage);
    const blockingReason = Object.keys(RESOURCE_NAMES).find(name => !this.free(usage[name]));
    const byOperations = freeA >= 4 ? (freeA - 2) * PART_BYTES : MULTIPART_THRESHOLD - 1;
    return {
      usage,
      notices: this.transitions(usage, blocked, demand),
      maxUploadBytes: usage.reliable && freeA && freeB ? Math.min(freeStorage, this.maxObject, byOperations) : 0,
      ...(blockingReason ? { blockingReason } : {}),
    };
  }

  async initialize() {
    if (this.meta("initialized", false)) return;
    if (!this.initialization) {
      this.initialization = this.bootstrap().finally(() => { this.initialization = null; });
    }
    return this.initialization;
  }

  async cleanLegacy() {
    // The pre-cutover runner can still write unregistered presigned downloads.
    if (this.meta("initialized", false) || this.initialization) return;
    let cursor;
    do {
      const now = Date.now();
      if (this.usageCounts(now).used_a >= this.limits.classA.limit) return;
      this.charge(now, 1, 0);
      const page = await this.env.DOWNLOADS.list({ prefix: DOWNLOAD_PREFIX, ...(cursor ? { cursor } : {}) });
      const cutoff = Date.now() - DOWNLOAD_RETENTION_MS;
      const expired = page.objects.filter(object => object.uploaded.getTime() <= cutoff).map(object => object.key);
      if (expired.length) await this.env.DOWNLOADS.delete(expired);
      cursor = page.truncated && cursor !== page.cursor ? page.cursor : undefined;
    } while (cursor);
  }

  async bootstrap() {
    if (!this.meta("baseline", false)) {
      this.ctx.storage.transactionSync(() => {
        this.charge(Date.now(), config(this.env, "R2_CLASS_A_BASELINE", 0, 1_000_000),
          config(this.env, "R2_CLASS_B_BASELINE", 0, 10_000_000));
        this.setMeta("baseline", true);
      });
    }
    let cursor = this.meta("bootstrap_cursor", null);
    // No guarded upload can start until inventory is complete. Record every attempted listing.
    do {
      const now = Date.now();
      const used = this.usageCounts(now).used_a;
      if (used >= this.limits.classA.limit) throw new Error("R2 inventory blocked by Class A limit");
      this.charge(now, 1, 0);
      const page = await this.env.DOWNLOADS.list(cursor ? { cursor } : {});
      if (page.truncated && (!page.cursor || page.cursor === cursor)) throw new Error("R2 inventory could not advance");
      cursor = page.truncated ? page.cursor : null;
      this.ctx.storage.transactionSync(() => {
        for (const object of page.objects) {
          const expires = object.key.startsWith(DOWNLOAD_PREFIX) ? object.uploaded.getTime() + DOWNLOAD_RETENTION_MS : null;
          this.sql.exec("INSERT OR IGNORE INTO resource_files(id,key,size,state,expires) VALUES (?,?,?,?,?)",
            `legacy:${object.key}`, object.key, object.size, "legacy", expires);
        }
        this.setMeta("bootstrap_cursor", cursor);
        if (!cursor) this.setMeta("initialized", true);
      });
    } while (cursor);
    await this.schedule();
  }

  async schedule() {
    const now = Date.now();
    const first = this.sql.exec("SELECT MIN(expires) AS expires FROM resource_files WHERE expires IS NOT NULL").one().expires;
    const blocked = this.meta("blocked", {});
    const hasBlockedMeter = blocked.classA || blocked.classB;
    const firstCharged = hasBlockedMeter ? this.sql.exec(`SELECT MIN(day) AS day FROM resource_day
      WHERE day>=? AND ((? AND used_a>0) OR (? AND used_b>0))`, oldestDay(now), Number(Boolean(blocked.classA)),
    Number(Boolean(blocked.classB))).one().day : null;
    const nextAllowance = firstCharged === null ? Infinity : (firstCharged + BILLING_GUARD_DAYS + 1) * DAY;
    const notices = this.env.RESOURCE_NOTICE_WORKFLOW &&
      (this.meta("notices", []).length || this.meta("notices_sending", []).length);
    const alert = notices ? Math.max(now + 1000, this.meta("notice_retry", 0)) : Infinity;
    const next = Math.min(first ?? Infinity, nextAllowance, alert);
    // Never overwrite an earlier alarm. It will check the latest persisted queue when it fires.
    if (Number.isFinite(next)) {
      const scheduled = await this.ctx.storage.getAlarm();
      if (scheduled === null || next < scheduled) await this.ctx.storage.setAlarm(Math.max(now, next));
    }
  }

  async alarm() {
    const now = Date.now();
    const rows = this.ctx.storage.transactionSync(() => {
      this.sql.exec("DELETE FROM resource_day WHERE day<?", oldestDay(now));
      const expired = this.sql.exec("SELECT * FROM resource_files WHERE expires <= ? ORDER BY expires LIMIT ?", now, EXPIRE_BATCH).toArray();
      for (const row of expired) {
        if (row.state === "pending") this.charge(now, row.pending_a, 0);
        this.sql.exec("UPDATE resource_files SET state='cleanup', pending_a=0, pending_b=0 WHERE id=?", row.id);
      }
      return expired;
    });
    const removable = [];
    for (const row of rows) {
      try {
        if (row.upload_id) await this.env.DOWNLOADS.resumeMultipartUpload(row.key, row.upload_id).abort();
      } catch (error) {
        if (!missingUpload(error)) {
          this.sql.exec("UPDATE resource_files SET expires=? WHERE id=?", now + 5 * MINUTE, row.id);
          continue;
        }
      }
      if (row.upload_id) this.sql.exec("UPDATE resource_files SET upload_id=NULL WHERE id=?", row.id);
      removable.push(row);
    }
    if (removable.length) {
      try {
        await this.env.DOWNLOADS.delete(removable.map(row => row.key));
        this.ctx.storage.transactionSync(() => {
          for (const row of removable) this.sql.exec("DELETE FROM resource_files WHERE id=? AND state='cleanup'", row.id);
        });
      } catch {
        console.warn("R2 cleanup will retry", { count: removable.length });
        for (const row of removable) this.sql.exec("UPDATE resource_files SET expires=? WHERE id=?", now + 5 * MINUTE, row.id);
      }
    }
    this.transitions(this.current(Date.now()), null, 0, false);
    if (Date.now() >= this.meta("notice_retry", 0)) {
      const sent = await sendBackgroundNotices(this.env, this.ctx.storage);
      if (!sent) this.setMeta("notice_retry", Date.now() + 5 * MINUTE);
      else if (this.meta("notice_retry", 0)) this.setMeta("notice_retry", 0);
    }
    await this.schedule();
  }

  reserve(input, now) {
    if (!UUID.test(input.id ?? "") || !validKey(input.key) || !integer(input.size, 1, 10_000_000_000)
      || !integer(input.classA, 1, 10_002)) return reply({ ok: false, reason: "invalid" }, 400);
    if (input.size > this.maxObject) return reply({ ok: false, reason: "size", ...this.snapshot(now) }, 413);
    const existing = this.object(input.id);
    if (existing) {
      if (existing.state === "pending" && existing.expires > now && existing.key === input.key
        && existing.size === input.size && existing.pending_a === input.classA) return reply({ ok: true, ...this.snapshot(now) });
      return reply({ ok: false, reason: "conflict" }, 409);
    }
    if (this.sql.exec("SELECT 1 FROM resource_files WHERE key=?", input.key).toArray().length) {
      return reply({ ok: false, reason: "conflict" }, 409);
    }
    const usage = this.current(now);
    const expected = { storage: input.size, classA: input.classA, classB: 1 };
    const blocked = Object.keys(expected).find(name => expected[name] > this.free(usage[name]));
    if (blocked) return reply({ ok: false, reason: blocked, ...this.snapshot(now, blocked, expected[blocked]) }, 429);
    this.sql.exec(`INSERT INTO resource_files(id,key,size,state,expires,pending_a,pending_b)
      VALUES (?,?,?,'pending',?,?,1)`, input.id, input.key, input.size, now + RESERVATION_TTL, input.classA);
    return reply({ ok: true, ...this.snapshot(now) });
  }

  complete(input, now) {
    const etag = typeof input.etag === "string" ? input.etag.replace(/^"|"$/g, "").toLowerCase() : "";
    const row = this.object(input.id);
    if (!row || row.expires <= now) return reply({ ok: false, reason: "expired" }, 410);
    if (row.state === "ready" && row.etag === etag && row.size === input.verifiedSize) {
      return reply({ ok: true, token: row.token, expiresAt: row.expires, ...this.snapshot(now) });
    }
    if (row.state !== "pending" || !integer(input.usedClassA, 1, row.pending_a)
      || input.verifiedSize !== row.size || !ETAG.test(etag)) return reply({ ok: false, reason: "invalid" }, 400);
    const access = token();
    const expires = now + DOWNLOAD_RETENTION_MS;
    this.charge(now, input.usedClassA, 0);
    this.sql.exec(`UPDATE resource_files SET state='ready', token=?, etag=?, expires=?, pending_a=0, upload_id=NULL WHERE id=?`,
      access, etag, expires, input.id);
    return reply({ ok: true, token: access, expiresAt: expires, ...this.snapshot(now) });
  }

  cancel(input, now) {
    const row = this.object(input.id);
    if (row && (row.state === "pending" || row.state === "ready")) {
      if (row.state === "pending") {
        const used = integer(input.usedClassA, 0, row.pending_a) ? input.usedClassA : row.pending_a;
        this.charge(now, used, 0);
      }
      this.sql.exec("UPDATE resource_files SET state='cleanup', expires=?, pending_a=0, pending_b=0 WHERE id=?", now, input.id);
    }
    return reply({ ok: true, ...this.snapshot(now) });
  }

  registerUpload(input, now) {
    const row = this.object(input.id);
    if (!row || row.state !== "pending" || row.expires <= now) return reply({ ok: false, reason: "expired" }, 410);
    if (typeof input.uploadId !== "string" || !input.uploadId || input.uploadId.length > 2048
      || (row.upload_id && row.upload_id !== input.uploadId)) return reply({ ok: false, reason: "invalid" }, 400);
    if (!row.upload_id) this.sql.exec("UPDATE resource_files SET upload_id=? WHERE id=?", input.uploadId, input.id);
    return reply({ ok: true });
  }

  read(input, now) {
    if (!DOWNLOAD_TOKEN.test(input.token ?? "") || !["GET", "HEAD"].includes(input.method)) return { status: 404 };
    const row = this.sql.exec("SELECT * FROM resource_files WHERE token=?", input.token).toArray()[0];
    if (!row) return { status: 404 };
    if (row.state !== "ready" || row.expires <= now) return { status: 410 };
    const plan = parseReadRequest(input, row);
    const response = { key: row.key, size: row.size, etag: row.etag, ...plan };
    if (plan.head || plan.status === 304 || plan.status === 416) return response;
    if (row.pending_b) {
      if (!this.chargeReservedRead(now)) return this.blockRead(now);
      this.sql.exec("UPDATE resource_files SET pending_b=0 WHERE id=?", row.id);
      return response;
    }
    const usage = this.readUsage(now);
    if (this.free(usage) < 1) return this.blockRead(now);
    const before = level(usage);
    this.charge(now, 0, 1);
    usage.used++;
    if (level(usage) > before) {
      const notices = this.transitions(this.current(now), null, 0, false);
      return { ...response, noticePending: notices.length > 0 };
    }
    return response;
  }

  blockRead(now) {
    this.transitions(this.current(now), "classB", 1, false);
    return { status: 429 };
  }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    let input = {};
    if (request.method === "POST") {
      try { input = await request.json(); } catch { return reply({ ok: false, reason: "invalid" }, 400); }
      if (!input || typeof input !== "object" || Array.isArray(input)) return reply({ ok: false, reason: "invalid" }, 400);
    }
    if (path === "/dispatch/lease" || path === "/dispatch/release") {
      return reply(await dispatchLease(this.ctx.storage, path, input));
    }
    if (path === "/legacy-clean") {
      await this.cleanLegacy();
      return reply({ ok: true });
    }
    if (path === "/read") {
      const result = this.ctx.storage.transactionSync(() => this.read(input, Date.now()));
      if (result.status === 429 || result.noticePending) await this.schedule();
      return reply(result);
    }
    try {
      await this.initialize();
    } catch {
      console.warn("R2 inventory temporarily unavailable");
      return reply({ ok: false, reason: "unavailable", usage: { reliable: false } }, 503);
    }
    if (path === "/maintenance") {
      await this.schedule();
      return reply({ ok: true });
    }
    // Reads and mutations below are synchronous inside one database transaction.
    const now = Date.now();
    const result = this.ctx.storage.transactionSync(() => {
      if (path === "/status") return reply({ ok: true, ...this.snapshot(now) });
      if (path === "/reserve") return this.reserve(input, now);
      if (!UUID.test(input.id ?? "")) return reply({ ok: false, reason: "invalid" }, 400);
      if (path === "/complete") return this.complete(input, now);
      if (path === "/cancel") return this.cancel(input, now);
      if (path === "/upload-id") return this.registerUpload(input, now);
      return reply({ ok: false, reason: "invalid" }, 404);
    });
    if (path === "/reserve" || path === "/complete" || path === "/cancel") await this.schedule();
    return result;
  }
}
