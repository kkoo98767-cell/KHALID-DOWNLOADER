import unittest

import yt_dlp

from app.main import youtube_authentication


COOKIE_FILE = (
    "# Netscape HTTP Cookie File\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tFALSE\t0\tYT_TEST\tsynthetic-youtube-value\n"
    ".example.org\tTRUE\t/\tTRUE\t0\tOTHER_TEST\tsynthetic-unrelated-value\n"
)


class YouTubeSessionTests(unittest.TestCase):
    def test_session_is_scoped_to_one_video_and_youtube_cookie_domain(self):
        url, stream = youtube_authentication(
            "https://youtu.be/VFE3slVdK3g?si=tracking&list=playlist", COOKIE_FILE
        )
        self.assertEqual(url, "https://www.youtube.com/watch?v=VFE3slVdK3g")
        with stream, yt_dlp.YoutubeDL({"cookiefile": stream, "allowed_extractors": ["youtube"]}) as ydl:
            self.assertEqual(ydl.cookiejar.get_cookie_header(url), "YT_TEST=synthetic-youtube-value")
            self.assertIsNone(ydl.cookiejar.get_cookie_header("https://example.org/"))
            self.assertIsNone(ydl.cookiejar.get_cookie_header("https://youtube.com.example.org/"))
            self.assertIsNone(ydl.cookiejar.get_cookie_header(url.replace("https://", "http://")))
            self.assertEqual(tuple(ydl._ies), ("Youtube",))

    def test_other_links_and_missing_session_keep_the_guest_path(self):
        for url in (
            "https://www.tiktok.com/@example/video/1",
            "https://youtube.com.example.org/watch?v=VFE3slVdK3g",
            "http://www.youtube.com/watch?v=VFE3slVdK3g",
            "https://www.youtube.com/redirect?v=VFE3slVdK3g",
            "https://www.youtube.com/playlist?list=PLexample",
        ):
            with self.subTest(url=url):
                self.assertIsNone(youtube_authentication(url, COOKIE_FILE))
        self.assertIsNone(youtube_authentication("https://youtu.be/VFE3slVdK3g", ""))

    def test_invalid_cookie_error_does_not_expose_secret_rows(self):
        invalid = COOKIE_FILE.replace("\t0\tYT_TEST", "\tsecret-expiry\tYT_TEST")
        with self.assertRaises(ValueError) as caught:
            youtube_authentication("https://youtu.be/VFE3slVdK3g", invalid)
        self.assertNotIn("secret-expiry", str(caught.exception))
        self.assertNotIn("synthetic-youtube-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
