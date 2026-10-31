"""Couch price samples exclude free cards, bundles, and conflicting asks."""

import unittest
import json
from pathlib import Path
import tempfile

from marketplace_couch_comps import approved_pricing_comparables, prepare_candidate


class CouchComparableTests(unittest.TestCase):
    def test_single_priced_couch_is_candidate(self):
        row = prepare_candidate({"listing_id": "1", "title": "Blue couch",
                                 "asking_price": 150,
                                 "description_status": "visible",
                                 "description": "Used, good condition"}, "today")
        self.assertEqual(row["review_status"], "candidate")
        self.assertEqual(row["subtype"], "sofa")
        self.assertEqual(row["effective_ask"], 150)

    def test_free_card_with_seller_ask_requires_review(self):
        row = prepare_candidate({"listing_id": "2", "title": "Couch",
                                 "asking_price": 0,
                                 "description_status": "visible",
                                 "description": "Asking $180."}, "today")
        self.assertEqual(row["effective_ask"], 180)
        self.assertEqual(row["review_status"], "review")
        self.assertIn("zero_card_seller_asks", row["review_reasons"])

    def test_bundle_and_placeholder_need_review(self):
        row = prepare_candidate({"listing_id": "3", "title": "Loveseat set",
                                 "asking_price": 1,
                                 "description_status": "visible"}, "today")
        self.assertEqual(row["review_status"], "review")
        self.assertIn("bundle_or_multiple_items", row["review_reasons"])
        self.assertIn("free_or_placeholder_price", row["review_reasons"])

    def test_new_claim_conflicting_with_seller_condition_needs_review(self):
        row = prepare_candidate({"listing_id": "4", "title": "Brand new sofa",
                                 "asking_price": 850,
                                 "seller_condition_label": "Used - like new",
                                 "description_status": "visible"}, "today")
        self.assertEqual(row["review_status"], "review")
        self.assertIn("title_and_condition_conflict", row["review_reasons"])

    def test_reviewed_pool_uses_only_clear_positive_single_item_prices(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "details.json"
            path.write_text(json.dumps({"requested_zip": "21009",
                "radius_miles_target": 30, "captured_at": "2026-10-26T20:00:00Z",
                "listings": [
                    {"listing_id": "1", "title": "Blue couch", "asking_price": 100,
                     "description_status": "visible", "url": "https://www.facebook.com/marketplace/item/1/"},
                    {"listing_id": "2", "title": "Free couch", "asking_price": 0,
                     "description_status": "visible"},
                    {"listing_id": "3", "title": "Couch and chairs", "asking_price": 300,
                     "description_status": "visible"},
                    {"listing_id": "4", "title": "Sleeper sectional sofa with storage and ports", "asking_price": 450,
                     "description_status": "visible"}]},
                ), encoding="utf-8")
            pool = approved_pricing_comparables(path)
        self.assertEqual([row["listing_id"] for row in pool], ["1"])
        self.assertTrue(pool[0]["price_reviewed"])
        self.assertEqual(pool[0]["search_zip"], "21009")


if __name__ == "__main__":
    unittest.main()
