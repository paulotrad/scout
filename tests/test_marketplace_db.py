"""Check that saved listings and image bytes survive a SQLite round trip."""

import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from marketplace_db import database_stats, get_image, import_run


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/V7sAAAAASUVORK5CYII="
)


class MarketplaceDatabaseTests(unittest.TestCase):
    def test_run_import_keeps_image_and_price_history_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_dir = root / "card_images"
            image_dir.mkdir()
            (image_dir / "123.png").write_bytes(PNG)
            run = {
                "captured_at": "2026-10-22T16:00:00+00:00",
                "comparables": [{"source": "craigslist", "listing_id": "cl-1",
                                 "title": "Leather sofa", "asking_price": 120,
                                 "search_zip": "21009"}],
                "listings": [{"listing_id": "123", "title": "Leather couch",
                              "description": "Seller says it is a sleeper sofa.",
                              "refactored_description": "Seller describes a sleeper sofa.",
                              "asking_price": 100, "search_zip": "21009",
                              "location": "Edgewood, MD",
                              "image_file": "card_images/123.png",
                              "identity": {"id": None},
                              "vision": {"status": "observed", "category": "furniture"},
                              "condition_assessment": {"status": "estimated", "result": {
                                  "usage_state": "used", "condition_grade": "good"}},
                              "valuation": {"status": "provisional"}}],
            }
            run_path = root / "run.json"
            run_path.write_text(json.dumps(run), encoding="utf-8")
            db_path = root / "marketplace.sqlite"
            first = import_run(run_path, db_path)
            self.assertEqual(first["images_stored"], 1)
            self.assertEqual(get_image(db_path, "facebook_marketplace", "123"),
                             ("image/png", PNG))
            self.assertEqual(database_stats(db_path)["listings"], 2)
            self.assertEqual(database_stats(db_path)["price_observations"], 2)
            self.assertEqual(database_stats(db_path)["image_bytes"], len(PNG))

            import_run(run_path, db_path)
            self.assertEqual(database_stats(db_path)["price_observations"], 2)
            run["captured_at"] = "2026-10-23T16:00:00+00:00"
            run["listings"][0]["asking_price"] = 80
            run_path.write_text(json.dumps(run), encoding="utf-8")
            import_run(run_path, db_path)
            self.assertEqual(database_stats(db_path)["price_observations"], 4)
            with closing(sqlite3.connect(db_path)) as db:
                prices = db.execute("""
                    SELECT asking_price FROM price_history
                    WHERE source = 'facebook_marketplace' AND listing_id = '123'
                    ORDER BY observed_at
                """).fetchall()
                evidence = db.execute("""
                    SELECT description, refactored_description, vision_json,
                           condition_assessment_json FROM listings
                    WHERE source = 'facebook_marketplace' AND listing_id = '123'
                """).fetchone()
            self.assertEqual(prices, [(100.0,), (80.0,)])
            self.assertEqual(evidence[0], "Seller says it is a sleeper sofa.")
            self.assertEqual(evidence[1], "Seller describes a sleeper sofa.")
            self.assertEqual(json.loads(evidence[2])["status"], "observed")
            self.assertEqual(json.loads(evidence[3])["result"]["usage_state"], "used")

            (image_dir / "123.png").unlink()
            run["listings"][0]["description"] = "Updated seller description"
            run_path.write_text(json.dumps(run), encoding="utf-8")
            import_run(run_path, db_path)
            self.assertEqual(get_image(db_path, "facebook_marketplace", "123"),
                             ("image/png", PNG))
            with closing(sqlite3.connect(db_path)) as db:
                saved_description = db.execute("""
                    SELECT description FROM listings WHERE source = ? AND listing_id = ?
                """, ("facebook_marketplace", "123")).fetchone()[0]
            self.assertEqual(saved_description, "Updated seller description")

    def test_run_import_rejects_image_outside_run_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = {"captured_at": "2026-10-22T16:00:00+00:00",
                   "listings": [{"listing_id": "123", "title": "Couch",
                                 "image_file": "../outside.png"}]}
            run_path = root / "run.json"
            run_path.write_text(json.dumps(run), encoding="utf-8")
            db_path = root / "marketplace.sqlite"
            with self.assertRaises(ValueError):
                import_run(run_path, db_path)
            self.assertEqual(database_stats(db_path)["listings"], 0)


if __name__ == "__main__":
    unittest.main()
