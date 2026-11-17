import base64
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from botocore.exceptions import ClientError

from app.delivery import (
    DeliveryError, DownloadGuard, ResourceBlocked, R2Upload, md5_digest,
    r2_upload, source_size, telegram_call, upload_plan,
)
from app.main import probe_duration, validate_duration


class MediaDeliveryTests(unittest.TestCase):
    @patch("app.delivery.requests.post")
    def test_ambiguous_telegram_disconnect_does_not_duplicate_the_send(self, post):
        post.side_effect = requests.ConnectionError("response was lost")
        with self.assertRaises(DeliveryError):
            telegram_call("test", "sendMessage", {"chat_id": "test", "text": "جاهز"})
        post.assert_called_once()

    @patch("app.delivery.time.sleep")
    @patch("app.delivery.requests.post")
    def test_telegram_429_only_retries_explicit_rate_rejections(self, post, sleep):
        refused, accepted = MagicMock(), MagicMock()
        refused.ok = False
        refused.json.return_value = {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
        accepted.json.return_value = {"ok": True, "result": {"message_id": 1}}
        post.side_effect = [refused, accepted]
        self.assertTrue(telegram_call("test", "sendMessage", {"chat_id": "test", "text": "جاهز"})["ok"])
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("app.main.subprocess.run")
    def test_short_video_is_not_hidden_by_normal_audio_duration(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps({
            "format": {"duration": "21.1"},
            "streams": [{"codec_type": "video", "duration": "0.2"},
                        {"codec_type": "audio", "duration": "21.1"}],
        }), "")
        self.assertEqual(probe_duration(Path("movie.mp4")), .2)
        with self.assertRaisesRegex(RuntimeError, "Incomplete download"):
            validate_duration(Path("movie.mp4"), 21)

    @patch("app.main.subprocess.run")
    def test_audio_only_file_cannot_be_delivered_as_video(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps({
            "format": {"duration": "21"}, "streams": [{"codec_type": "audio"}]
        }), "")
        with self.assertRaisesRegex(RuntimeError, "no readable video"):
            probe_duration(Path("movie.mp4"))

    def test_size_estimates_sum_selected_tracks_and_retain_uncertainty(self):
        info = {"requested_formats": [{"filesize": 80_000_000}, {"filesize_approx": 5_000_000}]}
        self.assertEqual(source_size(info), (85_000_000, True))
        info["requested_formats"][1].clear()
        self.assertEqual(source_size(info), (0, False))

    def test_guard_skips_r2_for_small_media_and_rejects_obviously_oversized_media(self):
        resources = MagicMock()
        resources.status.return_value = {"maxUploadBytes": 1_000_000_000,
                                         "usage": {"reliable": True}, "blockingReason": "storage"}
        guard = DownloadGuard(resources, ".")
        guard.local_limit = 3_000_000_000
        guard.match({"filesize": 8_000_000})
        resources.status.assert_not_called()
        with self.assertRaises(ResourceBlocked) as error:
            guard.match({"filesize_approx": 2_000_000_000})
        self.assertEqual(error.exception.reason, "storage")

    def test_preflight_checks_reserved_storage_and_operation_headroom(self):
        resources = MagicMock()
        usage = {"reliable": True,
                 "storage": {"used": 2_000_000_000, "reserved": 1_000_000_000,
                             "limit": 4_000_000_000, "allowance": 10_000_000_000},
                 "classA": {"used": 96, "reserved": 0, "limit": 99, "allowance": 100},
                 "classB": {"used": 0, "reserved": 0, "limit": 99, "allowance": 100}}
        resources.status.return_value = {"maxUploadBytes": 134_217_727, "usage": usage}
        guard = DownloadGuard(resources, ".")
        guard.local_limit = 3_000_000_000
        for size, reason in [(1_100_000_000, "storage"), (200_000_000, "classA")]:
            with self.subTest(reason=reason), self.assertRaises(ResourceBlocked) as blocked:
                guard.check(size)
            self.assertEqual(blocked.exception.reason, reason)
        self.assertEqual(upload_plan(134_217_727), (0, 1))
        self.assertEqual(upload_plan(134_217_728), (2, 4))

    @patch("app.delivery.r2_client_and_bucket")
    def test_verified_put_consumes_one_reserved_write_and_no_verification_read(self, setup):
        content = b"original video"
        digest = md5_digest(content)
        client = MagicMock()
        setup.return_value = client, "test-bucket"
        client.put_object.return_value = {"ETag": f'"{digest.hex()}"'}
        resources = MagicMock()
        resources.origin = "example.test"
        address = "https://example.test/download/" + "a" * 48
        resources.complete.return_value = {"url": address, "expiresAt": int((time.time() + 3600) * 1000)}
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "video.mp4"
            media.write_bytes(content)
            link, _ = r2_upload(media, len(content), resources)
        self.assertEqual(link, address)
        request = client.put_object.call_args.kwargs
        self.assertEqual(request["ContentMD5"], base64.b64encode(digest).decode("ascii"))
        self.assertEqual(request["ContentLength"], len(content))
        self.assertEqual(request["StorageClass"], "STANDARD")
        self.assertEqual(request["IfNoneMatch"], "*")
        self.assertEqual(resources.reserve.call_args.args[2:], (len(content), 1))
        self.assertEqual(resources.complete.call_args.args[1:4], (1, len(content), digest.hex()))
        client.get_object.assert_not_called()
        client.head_object.assert_not_called()

    @patch("app.delivery.r2_client_and_bucket")
    def test_bad_etag_cancels_reservation_without_exposing_a_link(self, setup):
        client = MagicMock()
        setup.return_value = client, "test-bucket"
        resources = MagicMock()
        client.put_object.return_value = {"ETag": '"ffffffffffffffffffffffffffffffff"'}
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "video.mp4"
            media.write_bytes(b"original video")
            with self.assertRaises(DeliveryError):
                r2_upload(media, media.stat().st_size, resources)
        resources.complete.assert_not_called()
        self.assertEqual(resources.cancel.call_args.args[1], 1)

    @patch("app.delivery.PART_BYTES", 4)
    @patch("app.delivery.PART_WORKERS", 1)
    @patch("app.delivery.time.sleep")
    def test_multipart_counts_a_transient_part_retry_and_preserves_part_order(self, sleep):
        client = MagicMock()
        client.create_multipart_upload.return_value = {"UploadId": "test-id"}
        first, second = md5_digest(b"test"), md5_digest(b"42")
        combined = md5_digest(first + second).hex() + "-2"
        client.upload_part.side_effect = [
            ClientError({"ResponseMetadata": {"HTTPStatusCode": 503}}, "UploadPart"),
            {"ETag": f'"{first.hex()}"'}, {"ETag": f'"{second.hex()}"'},
        ]
        client.complete_multipart_upload.return_value = {"ETag": f'"{combined}"'}
        resources = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "video.mp4"
            media.write_bytes(b"test42")
            upload = R2Upload(client, "test-bucket", "key", media, resources, "id", "video/mp4", 1)
            self.assertEqual(upload.multipart(2), combined)
        self.assertEqual(upload.used_class_a, 5)
        self.assertEqual([c.kwargs["PartNumber"] for c in client.upload_part.call_args_list], [1, 1, 2])
        resources.track_multipart.assert_called_once_with("id", "test-id")
        sleep.assert_called_once_with(1.1)


if __name__ == "__main__":
    unittest.main()
