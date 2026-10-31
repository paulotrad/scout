"""Multiple seller photos survive collection, storage, and model input."""

import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from marketplace_db import get_images, store_result
from marketplace_details import gallery_urls
from marketplace_vision import _ask_ollama, listing_images, photo_hash_prefix
from marketplace_web import public_listing


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/V7sAAAAASUVORK5CYII=")


class MarketplaceGalleryTests(unittest.TestCase):
    def test_gallery_ignores_related_photos_and_duplicate_sizes(self):
        base = "https://scontent.xx.fbcdn.net/v/picture.jpg"
        candidates = [
            {"alt": "Product photo of Ryobi mower", "src": base + "?size=720"},
            {"alt": "Product photo of Ryobi mower", "src": base + "?size=960"},
            {"alt": "Related mower in another city", "src":
             "https://scontent.xx.fbcdn.net/v/related.jpg"},
            {"alt": "Product photo of Ryobi mower", "src":
             "https://example.com/not-facebook.jpg"},
            {"alt": "Product photo of Ryobi mower", "src":
             "https://scontent.xx.fbcdn.net/v/second.jpg"},
        ]
        self.assertEqual(gallery_urls(candidates),
                         [base + "?size=720",
                          "https://scontent.xx.fbcdn.net/v/second.jpg"])

    def test_gallery_round_trip_and_public_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image_dir = root / "gallery_images"
            image_dir.mkdir()
            for index in range(3):
                (image_dir / f"123-{index}.png").write_bytes(PNG + bytes([index]))
            listing = {"listing_id": "123", "title": "Ryobi mower",
                       "image_file": "gallery_images/123-0.png",
                       "image_files": [f"gallery_images/123-{i}.png" for i in range(3)]}
            db = root / "marketplace.sqlite"
            store_result({"listings": [listing]}, root, db)
            saved = get_images(db, "facebook_marketplace", "123")
            self.assertEqual(len(saved), 3)
            self.assertEqual(saved[2][1], PNG + b"\x02")
            input_file = root / "details.json"
            input_file.write_text(json.dumps({"listings": [listing]}), encoding="utf-8")
            photos, source = listing_images(input_file, listing, db)
            self.assertEqual(source, "file")
            self.assertEqual(len(photos), 3)
            self.assertNotEqual(photo_hash_prefix(photos), photo_hash_prefix(photos[:1]))
            result = public_listing("scan-20261027-21009-15mi", listing)
            self.assertEqual(len(result["image_urls"]), 3)
            self.assertTrue(result["image_urls"][2].endswith("/images/123/2"))
            for path in image_dir.iterdir():
                path.unlink()
            photos, source = listing_images(input_file, listing, db)
            self.assertEqual(source, "sqlite")
            self.assertEqual(len(photos), 3)

    def test_ollama_receives_all_photos_in_one_message(self):
        response = Mock()
        response.json.return_value = {"message": {"content": "{}"}}
        session = Mock()
        session.post.return_value = response
        self.assertEqual(_ask_ollama(session, [b"first", b"second"], "describe",
                                      "qwen3-vl:8b-instruct", 30), {})
        images = session.post.call_args.kwargs["json"]["messages"][0]["images"]
        self.assertEqual([base64.b64decode(value) for value in images],
                         [b"first", b"second"])


if __name__ == "__main__":
    unittest.main()
