import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.delivery import (
    DOWNLOAD_OPENERS, TELEGRAM_DIRECT_UPLOAD_LIMIT, DeliveryError, TelegramRejected,
    deliver, download_message, usage_message,
)
from app.main import error_notice


def snapshot(*, storage=2_000_000_000, class_a=20, class_b=30, reserved_b=0, reliable=True):
    return {"usage": {"storage": {"used": storage, "reserved": 0,
                                  "limit": 9_900_000_000, "allowance": 10_000_000_000},
                      "classA": {"used": class_a, "reserved": 0, "limit": 99, "allowance": 100},
                      "classB": {"used": class_b, "reserved": reserved_b, "limit": 99, "allowance": 100},
                      "reliable": reliable}}


class DownloadMessagesTests(unittest.TestCase):
    def test_risk_is_the_maximum_across_all_resources(self):
        for value, emoji in [(69, "🟢"), (70, "🟡"), (84, "🟡"), (85, "🟠"), (94, "🟠"), (95, "🔴")]:
            with self.subTest(value=value):
                self.assertEqual(usage_message(snapshot(class_a=value))[0], emoji)
        self.assertEqual(usage_message(snapshot(storage=8_500_000_000))[0], "🟠")
        self.assertEqual(usage_message(snapshot(class_b=95))[0], "🔴")
        self.assertEqual(usage_message(snapshot(class_b=94, reserved_b=1))[0], "🔴")
        _, footer = usage_message(snapshot())
        self.assertIn("2.00 / 10 جيجابايت", footer)
        self.assertIn("9.9 جيجابايت", footer)

    def test_missing_counters_use_red_and_do_not_report_zero(self):
        indicator, footer = usage_message({})
        self.assertEqual(indicator, "🔴")
        self.assertIn("تعذّر", footer)
        self.assertNotIn("0.00", footer)

    def test_message_variants_keep_the_indicator_first_and_storage_last(self):
        status = snapshot(class_a=70)
        _, footer = usage_message(status)
        link = "https://example.test/download/opaque-token"
        for opener in DOWNLOAD_OPENERS:
            with self.subTest(opener=opener), patch("app.delivery.secrets.choice", return_value=opener):
                message = download_message("A video", 68_186_255, status, link=link)
                self.assertEqual(message.splitlines()[:2], ["🟡", opener])
                self.assertTrue(message.endswith(footer))
                self.assertIn(link, message)
                self.assertIn("ساعة", message)
                self.assertIn("يُحذف الملف تلقائيًا", message)

    def test_long_titles_fit_telegram_caption_utf16_budget(self):
        caption = download_message("🎬" * 500, 12_345_678, snapshot())
        self.assertLessEqual(len(caption.encode("utf-16-le")) // 2, 1024)

    @patch("app.delivery.r2_upload")
    @patch("app.delivery.telegram_call")
    def test_small_download_includes_usage_in_video_caption(self, telegram, r2):
        resources = MagicMock()
        resources.status.return_value = snapshot()
        with tempfile.TemporaryDirectory() as temp:
            media = Path(temp) / "video.mp4"
            media.write_bytes(b"video")
            deliver("token", "chat", media, "Title", resources)
        self.assertEqual(telegram.call_args.args[1], "sendVideo")
        self.assertTrue(telegram.call_args.args[2]["caption"].endswith(usage_message(snapshot())[1]))
        r2.assert_not_called()
        resources.status.assert_called_once()

    @patch("app.delivery.r2_upload")
    @patch("app.delivery.telegram_call")
    def test_large_download_uses_committed_usage_and_disables_link_preview(self, telegram, r2):
        media = MagicMock()
        media.stat.return_value.st_size = TELEGRAM_DIRECT_UPLOAD_LIMIT + 1
        resources = MagicMock()
        committed = snapshot(storage=4_500_000_000)
        r2.return_value = "https://example.test/download/private", committed
        deliver("token", "chat", media, "Title", resources)
        resources.status.assert_not_called()
        method, data = telegram.call_args.args[1:3]
        self.assertEqual(method, "sendMessage")
        self.assertTrue(data["text"].endswith(usage_message(committed)[1]))
        self.assertEqual(json.loads(data["link_preview_options"]), {"is_disabled": True})

    @patch("app.delivery.r2_upload")
    @patch("app.delivery.telegram_call")
    def test_only_definite_size_rejection_can_fall_back_to_r2(self, telegram, r2):
        resources = MagicMock()
        resources.status.return_value = snapshot()
        r2.return_value = "https://example.test/download/private", snapshot()
        with tempfile.TemporaryDirectory() as temp:
            media = Path(temp) / "video.mp4"
            media.write_bytes(b"video")
            telegram.side_effect = DeliveryError("إرسال غير مؤكد")
            with self.assertRaises(DeliveryError):
                deliver("token", "chat", media, "Title", resources)
            r2.assert_not_called()
            telegram.side_effect = [TelegramRejected(413, "Request Entity Too Large"), {"ok": True}]
            deliver("token", "chat", media, "Title", resources)
        r2.assert_called_once()
        self.assertEqual(telegram.call_args.args[1], "sendMessage")

    def test_unknown_failures_do_not_echo_secrets_or_english(self):
        message = error_notice(RuntimeError("secret-key-with-english-message"), False)
        self.assertRegex(message, r"[\u0600-\u06ff]")
        self.assertNotIn("secret-key", message)


if __name__ == "__main__":
    unittest.main()
