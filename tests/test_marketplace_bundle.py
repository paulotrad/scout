"""Furniture bundles use their component markets, not matching whole bundles."""

import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

from marketplace_bundle import extract_bundle
from marketplace_pricing import run
from marketplace_value import estimate_value


def peers():
    return [{"source": "facebook_marketplace", "listing_id": str(index),
             "title": name, "reference_type": kind, "search_zip": "21009",
             "reference_distance_miles": 5, "asking_price": price,
             "condition": "good", "location": "Belcamp, MD",
             "observed_at": datetime.now(timezone.utc).isoformat(),
             "price_reviewed": True}
            for index, (kind, name, price) in enumerate((
                ("chair", "Chair", 20), ("chair", "Chair", 30),
                ("chair", "Chair", 40), ("sofa", "Couch", 100),
                ("sofa", "Couch", 150), ("sofa", "Couch", 200)), 1)]


class BundleTests(unittest.TestCase):
    def test_quantities_and_product_types_are_kept_separate(self):
        bundle = extract_bundle({"title": "2 chairs and 1 couch"})
        self.assertEqual(bundle["status"], "confirmed")
        self.assertEqual([(p["product_type"], p["quantity"])
                          for p in bundle["components"]],
                         [("chair", 2), ("sofa", 1)])
        self.assertEqual(extract_bundle({"title": "Dining table with 6 chairs"})[
            "components"][1]["quantity"], 6)
        self.assertEqual([(p["product_type"], p["quantity"]) for p in
            extract_bundle({"title": "Washer and dryer set"})["components"]],
            [("washer", 1), ("dryer", 1)])
        self.assertEqual([(p["product_type"], p["quantity"]) for p in
            extract_bundle({"title": "2 night stand and dresser"})["components"]],
            [("nightstand", 2), ("dresser", 1)])
        self.assertEqual([(p["product_type"], p["quantity"]) for p in
            extract_bundle({"title": "Laura Ashley dresser and nightstand"})["components"]],
            [("dresser", 1), ("nightstand", 1)])
        self.assertEqual([(p["product_type"], p["quantity"]) for p in
            extract_bundle({"title": "Mesa y 3 sillas"})["components"]],
            [("dining_table", 1), ("dining_chair", 3)])
        self.assertEqual([(p["product_type"], p["quantity"]) for p in
            extract_bundle({"title": "Free papasan chair and foot stool"})[
                "components"]], [("chair", 1), ("ottoman", 1)])
        self.assertEqual([(p["product_type"], p["quantity"]) for p in
            extract_bundle({"title": "Recliner with ottoman"})["components"]],
            [("recliner_chair", 1), ("ottoman", 1)])
        self.assertIsNone(extract_bundle({"title": "Washer dryer combo"}))
        self.assertIsNone(extract_bundle({"title": "Sectional sofa"}))
        self.assertIsNone(extract_bundle({
            "title": "Old sewing table no machine in it but table is nice"}))
        self.assertIsNone(extract_bundle({
            "title": "Recliner", "description": "Very comfortable, needs cleaning."}))
        wingback = extract_bundle({"title": "Wing back chairs x 2"})["components"][0]
        self.assertEqual(wingback["quantity"], 2)
        self.assertEqual(wingback["product_type"], "accent_chair")
        self.assertIsNone(extract_bundle({"title": "Free couch",
                                          "description": "Only the couch is included."}))

    def test_component_medians_sum_without_a_whole_bundle_peer(self):
        target = {"listing_id": "target", "title": "2 chairs and 1 couch",
                  "search_zip": "21009"}
        bundle_peer = {**peers()[0], "listing_id": "bundle",
                       "title": "2 chairs and couch", "asking_price": 5}
        value = estimate_value(target, [*peers(), bundle_peer])
        self.assertEqual(value["status"], "provisional")
        self.assertEqual(value["price_type"], "itemized_asking_range")
        self.assertEqual(value["midpoint"], 210)
        self.assertEqual(value["asking_price_sample"], [140, 210, 280])
        self.assertEqual([(part["quantity"], part["unit_midpoint"])
                          for part in value["bundle_components"]],
                         [(2, 30), (1, 150)])

    def test_real_bundle_titles_are_not_valued_as_one_table_or_chair(self):
        for title in ("Mesa y 3 sillas", "Free papasan chair and foot stool"):
            with self.subTest(title=title):
                valuation = estimate_value({"title": title,
                                            "search_zip": "21009"}, peers())
                self.assertEqual(valuation["status"], "unvalued")
                self.assertEqual(valuation["reason"],
                                 "insufficient_bundle_component_comparables")

    def test_unknown_chair_count_blocks_total(self):
        value = estimate_value({"title": "Couch and chairs",
                                "search_zip": "21009"}, peers())
        self.assertEqual(value["status"], "unvalued")
        self.assertEqual(value["reason"], "bundle_quantities_unclear")

    def test_repricing_rates_combined_ask_and_pauses_separate_sale_ask(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "scan.json"
            pool = root / "approved.json"
            cache = root / "geocodes.json"
            pool.write_text(json.dumps(peers()), encoding="utf-8")
            cache.write_text(json.dumps({
                "21009": {"latitude": 39.47, "longitude": -76.30},
                "Belcamp, MD": {"latitude": 39.48, "longitude": -76.24}}),
                encoding="utf-8")
            source.write_text(json.dumps({"requested_zip": "21009", "listings": [
                {"listing_id": "bundle", "title": "2 chairs and 1 couch",
                 "asking_price": 120, "search_zip": "21009"},
                {"listing_id": "separate", "title": "2 chairs and 1 couch",
                 "description": "Available separately or together.",
                 "asking_price": 120, "search_zip": "21009"}]}), encoding="utf-8")
            result = run(source, root / "priced.json", pool_file=pool,
                         geocode_cache=cache, db_path=None)
        first, second = [row["valuation"] for row in result["listings"]]
        self.assertEqual(first["midpoint"], 210)
        self.assertEqual(first["price_category"]["code"], "best")
        self.assertEqual(first["price_category"]["confidence"], "low")
        self.assertEqual(second["midpoint"], 210)
        self.assertNotIn("price_category", second)
        self.assertEqual(second["price_category_reason"],
                         "bundle_asking_price_scope_unclear")


if __name__ == "__main__":
    unittest.main()
