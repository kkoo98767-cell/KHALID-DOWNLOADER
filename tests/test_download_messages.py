import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.main import (
    DOWNLOAD_OPENERS, TELEGRAM_DIRECT_UPLOAD_LIMIT, deliver, download_message,
    format_size, notify, storage_usage_footer,
)


class DownloadMessagesTests(unittest.TestCase):
    @patch("app.main.r2_client_and_bucket")
    def test_storage_totals_all_pages_and_includes_all_bucket_files(self, connect):
        s3 = MagicMock()
        connect.return_value = (s3, "downloads")
        paginator = s3.get_paginator.return_value
        paginator.paginate.return_value = [
            {"Contents": [{"Size": 68186255}]},
            {"Contents": [{"Size": 68186255}]},
        ]
        self.assertTrue(storage_usage_footer().endswith("136.37 MB"))
        s3.get_paginator.assert_called_once_with("list_objects_v2")
        paginator.paginate.assert_called_once_with(Bucket="downloads")

    @patch("app.main.r2_client_and_bucket")
    def test_empty_bucket_and_failed_lookup_are_distinct(self, connect):
        s3 = MagicMock()
        connect.return_value = (s3, "downloads")
        s3.get_paginator.return_value.paginate.return_value = [{}]
        self.assertTrue(storage_usage_footer().endswith("0 B"))
        connect.side_effect = RuntimeError("unavailable")
        footer = storage_usage_footer()
        self.assertIn("تعذرت قراءتها", footer)
        self.assertNotIn("0 B", footer)

    def test_message_variants_keep_the_storage_footer_last(self):
        footer = "🗄 مساحة السيرفر (R2): 136.37 MB"
        link = "https://example.test/video?signature=keep-this-intact"
        for opener in DOWNLOAD_OPENERS:
            with self.subTest(opener=opener), patch("app.main.secrets.choice", return_value=opener):
                message = download_message("A video", 68186255, footer, link=link)
                self.assertTrue(message.startswith(opener))
                self.assertTrue(message.endswith(footer))
                self.assertIn(link, message)
                self.assertIn("ساعة", message)

    def test_long_titles_fit_telegram_caption_utf16_budget(self):
        caption = download_message("🎬" * 500, 12345678,
                                   "🗄 مساحة السيرفر (R2): 999.99 TB",
                                   source="https://example.test/" + "x" * 2000)
        self.assertLessEqual(len(caption.encode("utf-16-le")) // 2, 1024)
        self.assertTrue(caption.endswith("999.99 TB"))
        self.assertEqual(format_size(0), "0 B")

    @patch("app.main.telegram_call")
    @patch("app.main.storage_usage_footer", return_value="R2: 20 MB")
    def test_small_download_includes_usage_in_video_caption(self, footer, telegram):
        with tempfile.TemporaryDirectory() as temp:
            media = Path(temp) / "video.mp4"
            media.write_bytes(b"video")
            deliver("token", "chat", media, "https://example.test/source", "Title")
        self.assertEqual(telegram.call_args.args[1], "sendVideo")
        self.assertTrue(telegram.call_args.args[2]["caption"].endswith("R2: 20 MB"))
        footer.assert_called_once()

    @patch("app.main.telegram_call")
    def test_large_download_measures_usage_after_upload(self, telegram):
        events = []
        media = MagicMock()
        media.stat.return_value.st_size = TELEGRAM_DIRECT_UPLOAD_LIMIT + 1
        def upload(path):
            events.append("uploaded")
            return "https://example.test/download?signature=original"
        def usage():
            events.append("measured")
            return "R2: 70 MB"
        with patch("app.main.r2_download_link", side_effect=upload), patch("app.main.storage_usage_footer", side_effect=usage):
            deliver("token", "chat", media, "https://example.test/source", "Title")
        self.assertEqual(events, ["uploaded", "measured"])
        self.assertEqual(telegram.call_args.args[1], "sendMessage")
        self.assertTrue(telegram.call_args.args[2]["text"].endswith("R2: 70 MB"))

    @patch("app.main.telegram_call")
    @patch("app.main.storage_usage_footer", return_value="R2: 70 MB")
    def test_failure_notification_also_keeps_usage_last(self, footer, telegram):
        notify("token", "chat", "failed" * 2000)
        message = telegram.call_args.args[2]["text"]
        self.assertTrue(message.endswith("R2: 70 MB"))
        self.assertLessEqual(len(message), 4096)


if __name__ == "__main__":
    unittest.main()
