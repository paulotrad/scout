"""Missing essential parts never inherit complete-item price ratings."""

import json
from pathlib import Path
import tempfile
import unittest

from marketplace_completeness import incomplete_product
from marketplace_price_research import _search_spec
from marketplace_pricing import run
from marketplace_value import estimate_value


class CompletenessTests(unittest.TestCase):
    def test_mower_without_deck_engine_and_wheels_is_incomplete(self):
        listing = {"title": "JOHN DEERE L120 RIDING MOWER,NO DECK",
                   "description": "Just chassis, body and transmission. No engine. "
                                  "No wheels or tires. No battery."}
        issue = incomplete_product(listing)
        self.assertEqual(issue["reason"], "missing_major_components")
        self.assertEqual(issue["missing_components"],
                         ["cutting deck", "engine", "wheels"])
        self.assertIsNone(_search_spec(listing)[0])

    def test_false_missing_claims_do_not_pause_pricing(self):
        self.assertIsNone(incomplete_product({
            "title": "Riding mower",
            "description": "No deck problems or engine issues. Runs well."}))
        self.assertIsNone(incomplete_product({
            "title": "Dresser", "description": "No missing drawers."}))
        self.assertIsNone(incomplete_product({
            "title": "Razor E95 electric scooter", "description": "No battery charger."}))
        self.assertIsNone(incomplete_product({
            "title": "Ariens riding mower",
            "description": "Needs work on the deck; it can also be used for parts."}))
        self.assertIsNone(incomplete_product({
            "title": "Cub Cadet riding mower",
            "description": "Fix up or use for parts; needs maintenance."}))
        self.assertEqual(incomplete_product({
            "title": "Dresser", "description": "Missing one drawer."})[
                "missing_components"], ["drawer"])

    def test_incomplete_reference_is_excluded_from_complete_mower_price(self):
        target = {"listing_id": "target", "title": "Riding lawn mower",
                  "search_zip": "21009", "identity": {"id": None}}
        comps = [{"source": "facebook_marketplace", "listing_id": str(index),
                  "title": "Riding mower", "reference_type": "riding_mower",
                  "search_zip": "21009", "reference_distance_miles": 10,
                  "asking_price": price, "condition": "good"}
                 for index, price in enumerate((200, 250), 1)]
        comps.append({**comps[0], "listing_id": "parts",
                      "title": "Riding mower no deck", "asking_price": 150})
        result = estimate_value(target, comps)
        self.assertEqual(result["status"], "unvalued")
        self.assertEqual(result["comparable_count"], 2)

    def test_repricing_clears_stale_best_price_even_without_a_price_pool(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "priced.json"
            pool = root / "approved.json"
            source.write_text(json.dumps({"requested_zip": "21009", "listings": [{
                "listing_id": "target", "title": "Riding mower no deck",
                "description": "No engine or wheels either", "asking_price": 150,
                "valuation": {"status": "provisional", "price_category": {
                    "label": "Best price"}}}]}), encoding="utf-8")
            pool.write_text("[]", encoding="utf-8")
            result = run(source, source, pool_file=pool, db_path=None)
        value = result["listings"][0]["valuation"]
        self.assertEqual(value["status"], "unvalued")
        self.assertEqual(value["reason"], "missing_major_components")
        self.assertNotIn("price_category", value)


if __name__ == "__main__":
    unittest.main()
