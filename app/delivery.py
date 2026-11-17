import base64
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from urllib.parse import quote, urlsplit

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectTimeoutError

from .resource_auth import resource_bearer_token


TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_DIRECT_UPLOAD_LIMIT = 50_000_000
PART_BYTES = 64 * 1024 * 1024
MULTIPART_MIN_BYTES = 128 * 1024 * 1024
PART_WORKERS = 3
USAGE_TTL = 30

RESOURCE_ERRORS = {
    "storage": "مساحة التخزين المتاحة ما تكفي لهذا المقطع حاليًا. جرّب بعد انتهاء صلاحية بعض الملفات.",
    "classA": "رصيد عمليات رفع الملفات ما يكفي لهذا الطلب. يستأنف الرفع تلقائيًا عند توفر الرصيد.",
    "classB": "رصيد عمليات قراءة الملفات ما يكفي لإصدار رابط تنزيل الآن. يستأنف تلقائيًا عند توفر الرصيد.",
    "size": "حجم المقطع يتجاوز الحد المتاح للتنزيل والرفع بأمان.",
    "unavailable": "تعذر التحقق من موارد التخزين حاليًا. أوقفنا الرفع لهذا الطلب إلى أن تتوفر قراءة موثوقة.",
}


class DeliveryError(RuntimeError):
    pass


class ResourceBlocked(DeliveryError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(RESOURCE_ERRORS.get(reason, RESOURCE_ERRORS["unavailable"]))


class TelegramRejected(DeliveryError):
    def __init__(self, code, description):
        self.code = code
        self.description = description.casefold()
        super().__init__("تيليجرام رفض إرسال المقطع أو الرسالة. جرّب الطلب لاحقًا.")

    @property
    def file_too_big(self):
        return self.code == 413 or (self.code == 400 and any(
            phrase in self.description for phrase in ("file is too big", "file too large", "file_size_invalid")
        ))

    @property
    def video_unsupported(self):
        return self.code == 400 and any(phrase in self.description for phrase in (
            "video_content_type_invalid", "wrong type of the web page content", "unsupported video",
        ))


def telegram_call(token, method, data, files=None):
    # A timeout or 5xx can occur after Telegram accepts the message. Only retry an explicit 429.
    for attempt in range(2):
        if files:
            for _, stream, _ in files.values():
                stream.seek(0)
        try:
            response = requests.post(
                f"{TELEGRAM_API}/bot{token}/{method}", data=data, files=files,
                timeout=(20, 300), allow_redirects=False,
            )
        except requests.RequestException:
            raise DeliveryError("تعذر تأكيد الإرسال إلى تيليجرام. شيّك المحادثة قبل إعادة الطلب.") from None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if response.ok and payload.get("ok") is True:
            return payload
        code = payload.get("error_code", response.status_code)
        parameters = payload.get("parameters")
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
        if code == 429 and attempt == 0 and type(retry_after) is int and 0 < retry_after <= 10:
            time.sleep(retry_after)
            continue
        raise TelegramRejected(code, str(payload.get("description", "")))


class ResourceClient:
    def __init__(self):
        base = os.environ.get("RESOURCE_API_URL", "").strip().rstrip("/")
        parsed = urlsplit(base)
        self.base = base if (
            parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and not parsed.path
        ) else ""
        self.origin = parsed.netloc
        self.session = requests.Session()
        self.latest = {}
        self.observed_at = None
        self.notices = []

    def _call(self, operation="", data=None):
        if not self.base:
            raise ResourceBlocked("unavailable")
        try:
            token = resource_bearer_token()
        except Exception:
            raise ResourceBlocked("unavailable") from None
        if not token:
            raise ResourceBlocked("unavailable")
        endpoint = self.base + "/internal/resources" + ("/" + operation if operation else "")
        headers = {"Authorization": f"Bearer {token}"}
        # Operations use a stable reservation ID and the same payload on replay.
        for attempt in range(2 if data is not None else 1):
            try:
                response = self.session.request(
                    "GET" if data is None else "POST", endpoint, json=data, headers=headers,
                    allow_redirects=False, timeout=(2, 4),
                )
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    if isinstance(payload.get("usage"), dict):
                        self.latest = payload
                        self.observed_at = time.monotonic()
                    notices = payload.get("notices", [])
                    if isinstance(notices, list):
                        self.notices.extend(notice for notice in notices if isinstance(notice, str))
                    if response.ok and payload.get("ok") is True:
                        return payload
                    if response.status_code < 500:
                        raise ResourceBlocked(payload.get("reason", "unavailable"))
            except requests.RequestException:
                pass
        raise ResourceBlocked("unavailable")

    def status(self):
        if self.observed_at is not None and time.monotonic() - self.observed_at < USAGE_TTL:
            return self.latest
        try:
            return self._call()
        except ResourceBlocked:
            self.latest = {}
            self.observed_at = time.monotonic()
            return self.latest

    def reserve(self, identifier, key, size, class_a):
        return self._call("reserve", {"id": identifier, "key": key, "size": size, "classA": class_a})

    def track_multipart(self, identifier, upload_id):
        self._call("upload-id", {"id": identifier, "uploadId": upload_id})

    def complete(self, identifier, count, size, etag, content_type):
        return self._call("complete", {"id": identifier, "usedClassA": count,
                                      "verifiedSize": size, "etag": etag, "contentType": content_type})

    def cancel(self, identifier, count):
        try:
            self._call("cancel", {"id": identifier, "usedClassA": count})
        except ResourceBlocked:
            print("R2 reservation cleanup deferred to the ledger.")

    def notify(self, token, chat_id):
        for notice in dict.fromkeys(self.notices):
            try:
                telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": notice[:1000]})
            except DeliveryError:
                print("Telegram resource alert was not confirmed.")


def resource_values(usage, key):
    value = usage.get(key)
    if isinstance(value, dict):
        fields = tuple(value.get(field) for field in ("used", "reserved", "limit", "allowance"))
        if all(type(number) is int for number in fields):
            used, reserved, limit, allowance = fields
            if used >= 0 and reserved >= 0 and 0 < limit <= allowance:
                return fields
    return None


def quota_ratio(usage, key):
    values = resource_values(usage, key)
    if values is None:
        return None
    used, reserved, _, allowance = values
    return (used + reserved) / allowance


def quota_headroom(usage, key):
    values = resource_values(usage, key)
    if values is None:
        return None
    used, reserved, limit, _ = values
    return max(0, limit - used - reserved)


def risk_level(ratio):
    if ratio is None or ratio >= .95:
        return 3
    if ratio >= .85:
        return 2
    return int(ratio >= .70)


def usage_message(snapshot):
    usage = snapshot.get("usage", {})
    ratios = [quota_ratio(usage, key) for key in ("storage", "classA", "classB")]
    reliable = usage.get("reliable") is True
    level = max(map(risk_level, ratios)) if reliable else 3
    indicator = "🟢🟡🟠🔴"[level]
    heading = "☁️ تخزين كلاودفلير"
    if not reliable or ratios[0] is None:
        return indicator, heading + "\nتعذّر التحقق من تقدير موثوق للمساحة حاليًا."
    used, reserved, limit, allowance = resource_values(usage, "storage")
    state = ("آمنة", "قريبة من الحد", "مرتفعة", "عند حد الحماية")[risk_level(ratios[0])]
    lines = [heading, f"المساحة المستخدمة تقديريًا: {used / 1e9:.2f} / {allowance / 1e9:g} جيجابايت"]
    if reserved:
        lines.append(f"المساحة المحجوزة لطلبات جارية: {format_size(reserved)}")
    if limit < allowance:
        lines.append(f"سقف الحماية للمشروع: {limit / 1e9:g} جيجابايت")
    lines.extend([f"الحالة: {state}", "تقدير داخلي للمشروع"])
    return indicator, "\n".join(lines)


DOWNLOAD_OPENERS = (
    "أبشر، المقطع صار جاهز 🎬",
    "تم تجهيز المقطع، تقدر تحمّله الآن ✨",
    "مقطعك جاهز، مشاهدة ممتعة 🍿",
    "خلصنا! هذا مقطعك بالجودة اللي وفّرها المصدر 🎞",
    "جهّزت لك المقطع كامل، تفضّل 🎥",
    "المقطع صار عندك، استمتع بالمشاهدة 🎬",
)


def format_size(size):
    return f"{size / 1e9:.2f} جيجابايت" if size >= 1e9 else f"{size / 1e6:.2f} ميغابايت"


def download_message(title, size, snapshot, *, link=None):
    indicator, footer = usage_message(snapshot)
    clean_title = re.sub(r"\s+", " ", str(title or "مقطع فيديو")).strip()[:150]
    # Remove unsafe control characters and directional overrides from untrusted video titles.
    clean_title = re.sub(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]", "", clean_title)
    lines = [indicator, secrets.choice(DOWNLOAD_OPENERS), "", clean_title,
             "الصورة والصوت من المصدر، بدون إعادة ضغط.", "📦 حجم الملف: " + format_size(size)]
    if link:
        reason = ("حجم المقطع أكبر من حد الإرسال المباشر." if size > TELEGRAM_DIRECT_UPLOAD_LIMIT
                  else "جهّزت لك رابط تحميل لأن إرسال الملف مباشرة تعذّر.")
        lines.extend(["", reason, "🔗 رابط التنزيل الخاص:", link,
                      "⏳ الرابط صالح لمدة ساعة، ويُحذف الملف تلقائيًا عند انتهاء المدة."])
    lines.extend(["", "──────────────", footer])
    return "\n".join(lines)


def numeric_bytes(value):
    if type(value) in (int, float) and math.isfinite(value) and value > 0:
        return int(value)
    return 0


def source_size(info):
    formats = info.get("requested_formats") or [info]
    sizes = [numeric_bytes(fmt.get("filesize")) for fmt in formats]
    if all(sizes):
        return sum(sizes), False
    sizes = [size or numeric_bytes(fmt.get("filesize_approx")) for size, fmt in zip(sizes, formats)]
    return (sum(sizes), True) if all(sizes) else (0, False)


def upload_plan(size):
    parts = (size + PART_BYTES - 1) // PART_BYTES if size >= MULTIPART_MIN_BYTES else 0
    return parts, parts + 2 if parts else 1


class DownloadGuard:
    def __init__(self, resources, directory):
        self.resources = resources
        # Source and prepared remux coexist temporarily on the runner.
        self.local_limit = shutil.disk_usage(directory).free // 2
        self.files = {}
        self.quota = None

    def check(self, size):
        if size > self.local_limit:
            raise ResourceBlocked("size")
        if size <= TELEGRAM_DIRECT_UPLOAD_LIMIT:
            return
        if self.quota is None:
            self.quota = self.resources.status()
        elif type(self.quota.get("maxUploadBytes")) is int and size > self.quota["maxUploadBytes"]:
            # Reuse the allowance within a job. Ask for a fresh observation only if it would block.
            self.quota = self.resources.status()
        quota = self.quota
        usage = quota.get("usage", {})
        maximum = quota.get("maxUploadBytes")
        if usage.get("reliable") is not True or type(maximum) is not int:
            raise ResourceBlocked("unavailable")
        if size > maximum:
            reason = quota.get("blockingReason") or quota.get("reason")
            if reason not in RESOURCE_ERRORS:
                _, writes = upload_plan(size)
                for key, needed in (("storage", size), ("classA", writes), ("classB", 1)):
                    available = quota_headroom(usage, key)
                    if available is not None and available < needed:
                        reason = key
                        break
            raise ResourceBlocked(reason if reason in RESOURCE_ERRORS else "size")

    def match(self, info, *, incomplete=False):
        if not incomplete:
            size, estimated = source_size(info)
            if size:
                # An approximate source is only rejected early when clearly over capacity.
                self.check(int(size * .85) if estimated else size - min(size // 100, 1_000_000))
        return None

    def progress(self, event):
        name = event.get("filename") or event.get("info_dict", {}).get("format_id")
        size = numeric_bytes(event.get("downloaded_bytes"))
        if name and size > self.files.get(name, 0):
            self.files[name] = size
            self.check(sum(self.files.values()))


def r2_client_and_bucket():
    account_id = os.environ.get("R2_ACCOUNT_ID", "").strip()
    bucket = os.environ.get("R2_BUCKET_NAME", "").strip()
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    if not (re.fullmatch(r"[a-fA-F0-9]{32}", account_id) and bucket and access_key and secret_key):
        raise DeliveryError("إعدادات تخزين الملفات غير متاحة حاليًا. تعذر إرسال الرابط.")
    return boto3.client(
        "s3", endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com", region_name="auto",
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4", connect_timeout=15, read_timeout=180,
                      retries={"total_max_attempts": 1}, request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"),
    ), bucket


def md5_digest(data):
    return hashlib.md5(data, usedforsecurity=False).digest()


def verify_etag(result, expected):
    actual = result.get("ETag", "").strip('"').lower()
    if actual != expected:
        raise DeliveryError("تعذر التحقق من سلامة الملف المرفوع. لم يُرسل رابط التنزيل.")
    return actual


def retryable_part_error(error):
    if isinstance(error, ConnectTimeoutError):
        return True
    return error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (429, 500, 502, 503, 504)


class R2Upload:
    def __init__(self, client, bucket, key, media_path, resources, identifier, content_type, retry_count):
        self.client, self.bucket, self.key = client, bucket, key
        self.media_path, self.resources, self.identifier = media_path, resources, identifier
        self.content_type = content_type
        self.retry_count = retry_count
        self.used_class_a = 0
        self.lock = Lock()

    def _operation(self, method, **parameters):
        with self.lock:
            self.used_class_a += 1
        return method(Bucket=self.bucket, Key=self.key, **parameters)

    def _take_retry(self, error):
        with self.lock:
            if self.retry_count and retryable_part_error(error):
                self.retry_count -= 1
                return True
        return False

    def _storage_metadata(self):
        return {"ContentType": self.content_type, "CacheControl": "private, no-store", "StorageClass": "STANDARD",
                "ContentDisposition": f"attachment; filename*=UTF-8''{quote(self.media_path.name, safe='')}"}

    def single(self, size):
        with self.media_path.open("rb") as stream:
            digest = hashlib.file_digest(stream, lambda: hashlib.md5(usedforsecurity=False)).digest()
            stream.seek(0)
            result = self._operation(self.client.put_object, Body=stream, IfNoneMatch="*",
                                     ContentLength=size, ContentMD5=base64.b64encode(digest).decode("ascii"),
                                     **self._storage_metadata())
        return verify_etag(result, digest.hex())

    def _part(self, upload_id, number):
        with self.media_path.open("rb") as stream:
            stream.seek((number - 1) * PART_BYTES)
            data = stream.read(PART_BYTES)
        digest = md5_digest(data)
        params = {"UploadId": upload_id, "PartNumber": number, "Body": data,
                  "ContentLength": len(data), "ContentMD5": base64.b64encode(digest).decode("ascii")}
        try:
            result = self._operation(self.client.upload_part, **params)
        except (ClientError, ConnectTimeoutError) as exc:
            if not self._take_retry(exc):
                raise
            time.sleep(1.1)  # R2 same-key rate limit
            result = self._operation(self.client.upload_part, **params)
        verify_etag(result, digest.hex())
        return digest, {"ETag": result["ETag"], "PartNumber": number}

    def multipart(self, count):
        entry = self._operation(self.client.create_multipart_upload, **self._storage_metadata())
        upload_id = entry["UploadId"]
        try:
            self.resources.track_multipart(self.identifier, upload_id)
            parts = []
            with ThreadPoolExecutor(max_workers=PART_WORKERS) as pool:
                for start in range(1, count + 1, PART_WORKERS):
                    batch = range(start, min(count + 1, start + PART_WORKERS))
                    parts.extend(pool.map(lambda number: self._part(upload_id, number), batch))
            combined = md5_digest(b"".join(digest for digest, _ in parts)).hex() + f"-{count}"
            result = self._operation(self.client.complete_multipart_upload, UploadId=upload_id, IfNoneMatch="*",
                                     MultipartUpload={"Parts": [part for _, part in parts]})
            return verify_etag(result, combined)
        except Exception:
            try:
                self.client.abort_multipart_upload(Bucket=self.bucket, Key=self.key, UploadId=upload_id)
            except Exception:
                print("Incomplete multipart cleanup deferred to the ledger or bucket lifecycle.")
            raise


def verify_guarded_link(link, media_path, size):
    try:
        headers = requests.head(link, timeout=(3, 8), allow_redirects=False)
        if headers.status_code != 200 or headers.headers.get("Content-Length") != str(size):
            raise ValueError("Unexpected HEAD response.")
        with requests.get(link, headers={"Range": "bytes=0-0"}, stream=True,
                          timeout=(3, 8), allow_redirects=False) as response:
            if response.status_code != 206 or response.headers.get("Content-Range") != f"bytes 0-0/{size}":
                raise ValueError("Unexpected Range response.")
            with media_path.open("rb") as media:
                if response.raw.read(1) != media.read(1):
                    raise ValueError("Unexpected file byte.")
    except (ValueError, OSError, requests.RequestException):
        raise DeliveryError("تعذر التحقق من أن رابط التنزيل يفتح الملف كاملًا.") from None
    print("Guarded link smoke check passed: HEAD and one-byte Range.")


def r2_upload(media_path, size, resources):
    client, bucket = r2_client_and_bucket()
    identifier = str(uuid.uuid4())
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", media_path.name)[:180] or "video"
    key = f"khalid-downloads/{identifier}/{filename}"
    content_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
    parts, base_operations = upload_plan(size)
    free_writes = quota_headroom(resources.latest.get("usage", {}), "classA")
    retry_count = int(bool(parts) and (free_writes is None or free_writes > base_operations))
    try:
        resources.reserve(identifier, key, size, base_operations + retry_count)
    except ResourceBlocked as exc:
        if not retry_count or exc.reason != "classA":
            raise
        # The upload can still fit when the only unavailable operation was an optional retry.
        retry_count = 0
        resources.reserve(identifier, key, size, base_operations)
    upload = R2Upload(client, bucket, key, media_path, resources, identifier, content_type, retry_count)
    try:
        started = time.monotonic()
        etag = upload.multipart(parts) if parts else upload.single(size)
        registered = resources.complete(identifier, upload.used_class_a, size, etag, content_type)
    except DeliveryError:
        resources.cancel(identifier, upload.used_class_a)
        raise
    except Exception:
        resources.cancel(identifier, upload.used_class_a)
        raise DeliveryError("واجهنا مشكلة أثناء رفع الملف. لم يُرسل رابط تنزيل غير مؤكد.") from None
    link = registered.get("url")
    expires = registered.get("expiresAt")
    parsed = urlsplit(link if isinstance(link, str) else "")
    if (parsed.scheme != "https" or parsed.netloc != resources.origin or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not re.fullmatch(r"/download/[a-f0-9]{48}", parsed.path)
            or type(expires) is not int or expires <= time.time() * 1000):
        resources.cancel(identifier, upload.used_class_a)
        raise DeliveryError("تعذر التحقق من رابط التنزيل الخاص.")
    print(f"R2 upload validated in {time.monotonic() - started:.2f}s: "
          f"{size} bytes, {upload.used_class_a} attempted Class A operations.")
    if os.environ.get("VERIFY_R2_LINK") == "true":
        try:
            verify_guarded_link(link, media_path, size)
        except DeliveryError:
            resources.cancel(identifier, upload.used_class_a)
            raise
    return link, registered


def deliver(token, chat_id, media_path, title, resources):
    size = media_path.stat().st_size
    if size <= TELEGRAM_DIRECT_UPLOAD_LIMIT:
        caption = download_message(title, size, resources.status())
        data = {"chat_id": chat_id, "caption": caption}
        with media_path.open("rb") as stream:
            try:
                if media_path.suffix.lower() == ".mp4":
                    try:
                        telegram_call(token, "sendVideo", {**data, "supports_streaming": "true"},
                                      {"video": (media_path.name, stream, "video/mp4")})
                        print("Direct Telegram video accepted.")
                        return
                    except TelegramRejected as exc:
                        if not exc.video_unsupported:
                            raise
                telegram_call(token, "sendDocument", data,
                              {"document": (media_path.name, stream, "application/octet-stream")})
                print("Direct Telegram document accepted.")
                return
            except TelegramRejected as exc:
                if not exc.file_too_big:
                    raise
    link, snapshot = r2_upload(media_path, size, resources)
    message = download_message(title, size, snapshot, link=link)
    telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": message,
                                          "link_preview_options": json.dumps({"is_disabled": True})})
    print("Telegram accepted the guarded R2 download link.")
