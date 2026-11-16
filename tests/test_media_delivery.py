import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from app.main import probe_duration, r2_download_link, telegram_call, validate_duration, verify_download_link


def media_response(*chunks):
    response = MagicMock()
    response.__enter__.return_value = response
    response.iter_content.return_value = iter(chunks)
    return response


class MediaDeliveryTests(unittest.TestCase):
    @patch("app.main.time.sleep")
    @patch("app.main.requests.post")
    def test_temporary_telegram_disconnect_is_retried(self, post, sleep):
        response = MagicMock()
        response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        post.side_effect = [requests.ConnectionError("temporary disconnect"), response]
        self.assertTrue(telegram_call("test", "sendMessage", {"chat_id": "test", "text": "ready"})["ok"])
        self.assertEqual(post.call_count, 2)

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

    @patch("app.main.requests.get")
    def test_exact_download_url_must_match_all_original_bytes(self, get):
        data = b"a complete video payload"
        get.return_value = media_response(data[:5], data[5:])
        verify_download_link("https://example.test/private", len(data), hashlib.sha256(data).hexdigest())
        get.assert_called_once_with("https://example.test/private", stream=True, timeout=(30, 900))

    @patch("app.main.requests.get")
    def test_same_size_corruption_and_truncation_are_rejected(self, get):
        data = b"original video"
        for broken in [b"corrupt! video", data[:-1], data + b"x"]:
            with self.subTest(broken=broken):
                get.return_value = media_response(broken)
                with self.assertRaises(RuntimeError):
                    verify_download_link("https://example.test/private", len(data), hashlib.sha256(data).hexdigest())

    @patch.dict(os.environ, {"R2_ACCOUNT_ID": "test", "R2_BUCKET_NAME": "test-bucket",
                           "R2_ACCESS_KEY_ID": "test", "R2_SECRET_ACCESS_KEY": "test"})
    @patch("app.main.verify_download_link")
    @patch("app.main.boto3.client")
    def test_corrupt_upload_is_deleted_and_retried_before_link_release(self, client, verify):
        s3 = client.return_value
        s3.generate_presigned_url.return_value = "https://example.test/private"
        verify.side_effect = [RuntimeError("corrupt"), None]
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "video.mp4"
            media.write_bytes(b"original video")
            self.assertEqual(r2_download_link(media), "https://example.test/private")
        self.assertEqual(s3.upload_file.call_count, 2)
        s3.delete_object.assert_called_once()
        config = client.call_args.kwargs["config"]
        self.assertEqual(config.request_checksum_calculation, "when_required")
        self.assertEqual(config.response_checksum_validation, "when_required")

    @patch.dict(os.environ, {"R2_ACCOUNT_ID": "test", "R2_BUCKET_NAME": "test-bucket",
                           "R2_ACCESS_KEY_ID": "test", "R2_SECRET_ACCESS_KEY": "test"})
    @patch("app.main.verify_download_link", side_effect=RuntimeError("corrupt"))
    @patch("app.main.boto3.client")
    def test_repeated_corruption_never_releases_a_link(self, client, verify):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "video.mp4"
            media.write_bytes(b"original video")
            with self.assertRaisesRegex(RuntimeError, "failed twice"):
                r2_download_link(media)
        self.assertEqual(client.return_value.upload_file.call_count, 2)
        self.assertEqual(client.return_value.delete_object.call_count, 2)


if __name__ == "__main__":
    unittest.main()
