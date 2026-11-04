"""Тесты сохранения фото рассылок."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="veresk-media-")
os.environ["DATABASE_PATH"] = str(Path(_TMP) / "veresk.db")
os.environ.setdefault("BOT_TOKEN", "0:test")
os.environ.setdefault("POSIFLORA_USERNAME", "u")
os.environ.setdefault("POSIFLORA_PASSWORD", "p")
os.environ.setdefault("POSIFLORA_STORE_ID", "1")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")
os.environ.setdefault("MINIAPP_URL", "https://example.com/miniapp/")

from campaign_media import save_campaign_photo, sniff_image_kind  # noqa: E402

# 1×1 PNG
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
# минимальный JPEG SOI
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


class CampaignMediaTests(unittest.TestCase):
    def test_sniff_png(self):
        self.assertEqual(sniff_image_kind(_PNG), "png")

    def test_sniff_jpeg(self):
        self.assertEqual(sniff_image_kind(_JPEG), "jpeg")

    def test_save_with_image_mime(self):
        meta = save_campaign_photo(_PNG, filename="a.png", mime="image/png")
        self.assertTrue(meta["media_path"].endswith(".png"))
        self.assertEqual(meta["media_mime"], "image/png")

    def test_save_octet_stream_with_ext(self):
        meta = save_campaign_photo(
            _JPEG, filename="IMG_001.JPG", mime="application/octet-stream"
        )
        self.assertTrue(meta["media_path"].endswith(".jpg") or meta["media_path"].endswith(".jpeg"))
        self.assertEqual(meta["media_mime"], "image/jpeg")

    def test_save_octet_stream_by_magic(self):
        meta = save_campaign_photo(_PNG, filename="blob", mime="application/octet-stream")
        self.assertTrue(meta["media_path"].endswith(".png"))
        self.assertEqual(meta["media_mime"], "image/png")

    def test_reject_non_image(self):
        with self.assertRaises(ValueError) as ctx:
            save_campaign_photo(b"not-an-image", filename="notes.txt", mime="text/plain")
        self.assertEqual(str(ctx.exception), "only_images")

    def test_reject_empty(self):
        with self.assertRaises(ValueError) as ctx:
            save_campaign_photo(b"", filename="a.jpg", mime="image/jpeg")
        self.assertEqual(str(ctx.exception), "empty_file")


if __name__ == "__main__":
    unittest.main()
