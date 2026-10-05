import unittest

from fastapi import HTTPException

from app import main


class MediaUrlValidationTests(unittest.TestCase):
    def assert_youtube(self, pasted, video_id="dQw4w9WgXcQ"):
        url, platform = main._validate_media_url(pasted)
        self.assertEqual(platform, "youtube")
        self.assertEqual(url, f"https://www.youtube.com/watch?v={video_id}")

    def assert_rejected(self, pasted, fragment):
        with self.assertRaises(HTTPException) as caught:
            main._validate_media_url(pasted)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn(fragment, caught.exception.detail)

    def test_every_single_video_link_shape_is_accepted(self):
        for pasted in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?si=Ab12Cd34",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ&si=x",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/live/dQw4w9WgXcQ?feature=share",
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
        ):
            with self.subTest(pasted=pasted):
                self.assert_youtube(pasted)

    def test_links_without_a_scheme_are_accepted(self):
        for pasted in (
            "youtu.be/dQw4w9WgXcQ",
            "www.youtube.com/watch?v=dQw4w9WgXcQ",
            "youtube.com/watch?v=dQw4w9WgXcQ",
            "  youtu.be/dQw4w9WgXcQ  ",
        ):
            with self.subTest(pasted=pasted):
                self.assert_youtube(pasted)

    def test_share_text_around_the_link_is_ignored(self):
        self.assert_youtube("Dengerin ini deh https://youtu.be/dQw4w9WgXcQ?si=x mantap")
        self.assert_youtube("Judul lagu\nyoutu.be/dQw4w9WgXcQ.")

    def test_playlist_context_is_dropped_so_only_the_video_is_fetched(self):
        self.assert_youtube(
            "https://www.youtube.com/watch?v=kJQP7kiw5Fk&list=RDkJQP7kiw5Fk&start_radio=1",
            "kJQP7kiw5Fk",
        )

    def test_youtube_pages_that_are_not_one_video_are_rejected(self):
        for pasted in (
            "https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG",
            "https://www.youtube.com/@RickAstleyYT",
            "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
            "https://www.youtube.com/",
            "https://www.youtube.com/watch?v=short",
            "https://youtu.be/",
        ):
            with self.subTest(pasted=pasted):
                self.assert_rejected(pasted, "satu video YouTube")

    def test_tiktok_links_keep_working(self):
        for pasted in (
            "https://www.tiktok.com/@someone/video/7234567890123456789",
            "vm.tiktok.com/ZMabcdef/",
        ):
            with self.subTest(pasted=pasted):
                url, platform = main._validate_media_url(pasted)
                self.assertEqual(platform, "tiktok")
                self.assertTrue(url.startswith("https://"))
        self.assert_rejected("https://www.tiktok.com/@someone", "bukan link profil")

    def test_unsafe_or_foreign_links_are_rejected(self):
        for pasted in (
            "",
            "   ",
            "https://example.com/watch?v=dQw4w9WgXcQ",
            "ftp://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://user:pass@www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com:8443/watch?v=dQw4w9WgXcQ",
        ):
            with self.subTest(pasted=pasted):
                with self.assertRaises(HTTPException) as caught:
                    main._validate_media_url(pasted)
                self.assertEqual(caught.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
