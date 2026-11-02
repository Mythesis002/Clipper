import os
import tempfile
import unittest
from unittest.mock import patch

from youtube_uploader import sanitize_youtube_title, upload_to_youtube


class YouTubeUploaderTests(unittest.TestCase):
    def test_title_sanitizes_emoji_and_whitespace(self):
        title = sanitize_youtube_title("  Amazing 🚀\nPodcast  Clip  ")
        self.assertEqual(title, "Amazing Podcast Clip")
        self.assertLessEqual(len(title), 100)

    def test_title_is_bounded_without_cutting_to_empty(self):
        title = sanitize_youtube_title("word " * 40)
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title)

    def test_missing_oauth_secret_fails_with_actionable_error(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            video.write(b"fake mp4 bytes")
            video.flush()
            with patch.dict(os.environ, {"YT_TOKEN_JSON": ""}):
                with self.assertRaisesRegex(RuntimeError, "YT_TOKEN_JSON"):
                    upload_to_youtube(video.name, "Test")

    def test_missing_video_file_fails_before_auth(self):
        with patch.dict(os.environ, {"YT_TOKEN_JSON": "not-used"}):
            with self.assertRaises(FileNotFoundError):
                upload_to_youtube("missing-test-video.mp4", "Test")


if __name__ == "__main__":
    unittest.main()
