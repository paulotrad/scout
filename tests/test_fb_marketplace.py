"""Meaningful offline checks for card extraction and cautious valuation."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fb_marketplace import _comp_searches, keyword_routes, load_settings, parse_card, run
from marketplace_db import database_stats
from marketplace_value import (attach_identity_and_value, furniture_signature,
                               identify_item, infer_condition)
from product_candidate import suggest_product_candidate


ROOT = Path(__file__).resolve().parents[1]


class MarketplaceTests(unittest.TestCase):
    def test_card_fields_from_user_style_grid(self):
        raw = json.loads((ROOT / "data/facebook_cards_demo.json").read_text(encoding="utf-8"))
        cards = [parse_card(row) for row in raw]
        self.assertEqual(cards[0]["title"], "2015 BMW 3 series 328i xDrive Sedan")
        self.assertEqual(cards[0]["location"], "Landenberg, PA")
        self.assertEqual(cards[0]["asking_price"], 8000.0)
        self.assertEqual(cards[-1]["asking_price"], 0)
        self.assertEqual(cards[-1]["location"], "Edgewood, MD")

    def test_free_card_with_previous_price_uses_product_title(self):
        card = parse_card({
            "url": "https://www.facebook.com/marketplace/item/1112105637980015/",
            "text": "Free\n$30\nBrio Water Dispenser\nHavre de Grace, MD",
        })
        self.assertEqual(card["asking_price"], 0)
        self.assertEqual(card["previous_price"], 30)
        self.assertEqual(card["title"], "Brio Water Dispenser")
        self.assertEqual(card["original_card_title"], "$30")

    def test_identity_does_not_invent_quest_model(self):
        self.assertIsNone(identify_item("Meta quest- almost new")["id"])
        self.assertEqual(infer_condition("Meta quest- almost new")["grade"], "like_new")
        self.assertEqual(identify_item("GEFORCE RTX 5060Ti 16gb")["id"],
                         "gpu:nvidia-rtx-5060-ti-16gb")

    def test_estimate_requires_distinct_comparable_asking_prices(self):
        target = {"listing_id": "target", "title": "GeForce RTX 5060 Ti 16GB like new",
                  "asking_price": 400.0}
        self.assertEqual(attach_identity_and_value([target])[0]["valuation"]["status"],
                         "unvalued")
        item_id = "gpu:nvidia-rtx-5060-ti-16gb"
        comps = [{"listing_id": str(n), "item_id": item_id, "asking_price": price,
                  "condition": "good"}
                 for n, price in enumerate((300, 330, 360), 1)]
        valued = attach_identity_and_value([target], external_comps=comps)[0]
        self.assertEqual(valued["valuation"]["status"], "estimated")
        self.assertEqual(valued["valuation"]["comparable_count"], 3)
        self.assertLess(valued["valuation"]["low"], valued["valuation"]["high"])
        self.assertEqual(valued["valuation"]["confidence"], "low")
        duplicated = attach_identity_and_value([target], external_comps=comps[:2] +
                                               [comps[1], comps[1]])[0]
        self.assertEqual(duplicated["valuation"]["status"], "unvalued")

    def test_catalog_retail_reference_is_clearly_provisional(self):
        catalog = [{"id": "tool:demo-x", "name": "Demo Tool X", "category": "tool",
                    "aliases": ["Demo Tool X"], "new_price_usd": 100}]
        listing = {"listing_id": "item", "title": "Demo Tool X almost new",
                   "asking_price": 15}
        valued = attach_identity_and_value([listing], catalog=catalog)[0]
        self.assertEqual(valued["identity"]["id"], "tool:demo-x")
        self.assertEqual(valued["valuation"]["status"], "provisional")
        self.assertEqual(valued["valuation"]["confidence"], "very_low")

    def test_saved_zip_and_keywords_feed_offline_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "marketplace_settings.json"
            settings_file.write_text(json.dumps({"zip_code": "21009",
                "radius_miles": 15, "keywords": ["free"]}), encoding="utf-8")
            settings = load_settings(settings_file)
            self.assertEqual(settings["zip_code"], "21009")
            self.assertIn("free", settings["keywords"])
            self.assertEqual(load_settings(settings_file,
                                           radius_override=15)["radius_miles"], 15)
            output = Path(directory) / "output.json"
            db_path = Path(directory) / "marketplace.sqlite"
            result = run(None, ROOT / "data/facebook_cards_demo.json", settings,
                         None, None, output, 40, 0, 0, True, 0,
                         db_path=db_path)
            self.assertEqual(result["listing_count"], 4)
            self.assertEqual(result["requested_zip"], "21009")
            self.assertTrue(output.is_file())
            self.assertEqual(result["valued_count"], 0)
            self.assertEqual(database_stats(db_path)["listings"], 4)
            self.assertEqual(database_stats(db_path)["images"], 0)

    def test_product_candidate_marks_broad_types_without_inventing_a_model(self):
        desk = suggest_product_candidate("Solid wood desk")
        self.assertEqual(desk["types"][0]["type"], "desk")
        self.assertEqual(desk["material_clues"], ["solid_wood"])
        self.assertFalse(desk["needs_review"])
        bundle = suggest_product_candidate("Free piano and China cabinet")
        self.assertEqual(len(bundle["types"]), 2)
        self.assertTrue(bundle["needs_review"])
        self.assertEqual(suggest_product_candidate("Free")["types"], [])

    def test_local_furniture_range_uses_same_form_material_and_zip(self):
        target = {"listing_id": "target", "title": "Free leather couch",
                  "asking_price": 0, "search_zip": "21009"}
        comps = [
            {"source": "facebook_marketplace", "listing_id": "fb-1",
             "title": "Brown leather sofa", "asking_price": 100,
             "search_zip": "21009", "source_url": "https://www.facebook.com/marketplace/item/1/"},
            {"source": "craigslist", "listing_id": "cl-1",
             "title": "Leather couch", "asking_price": 140,
             "search_zip": "21009"},
            {"source": "craigslist", "listing_id": "cl-2",
             "title": "Black leather sofa", "asking_price": 180,
             "search_zip": "21009"},
            {"source": "craigslist", "listing_id": "cl-3",
             "title": "Faux leather couch", "asking_price": 40,
             "search_zip": "21009"},
            {"source": "craigslist", "listing_id": "cl-4",
             "title": "Leather sectional sofa", "asking_price": 900,
             "search_zip": "21009"},
            {"source": "craigslist", "listing_id": "cl-5",
             "title": "Leather couch", "asking_price": 300,
             "search_zip": "90210"},
            {"source": "craigslist", "listing_id": "cl-6",
             "title": "Brand new leather sofa", "asking_price": 999,
             "search_zip": "21009"},
        ]
        value = attach_identity_and_value([target], external_comps=comps)[0]["valuation"]
        self.assertEqual(value["status"], "provisional")
        self.assertEqual(value["price_type"], "local_asking_range")
        self.assertEqual(value["comparable_count"], 3)
        self.assertEqual(value["midpoint"], 140)
        self.assertEqual(value["low"], 120)
        self.assertEqual(value["high"], 160)
        self.assertEqual(value["sources"], ["craigslist", "facebook_marketplace"])
        self.assertEqual(furniture_signature("Leather sectional sofa")["product_type"],
                         "sectional")
        self.assertEqual(furniture_signature("Pull out couch")["product_type"],
                         "sofa_bed")
        self.assertIsNone(furniture_signature("Sofa and loveseat set"))

    def test_supplied_craigslist_comps_flow_through_offline_run(self):
        settings = load_settings(ROOT / "marketplace_settings.example.json")
        cards = [{"url": "https://www.facebook.com/marketplace/item/999/",
                  "text": "$0\nLeather couch\nEdgewood, MD"}]
        comps = [{"source": "craigslist", "listing_id": str(index),
                  "title": "Leather sofa", "asking_price": price,
                  "search_zip": "21009"}
                 for index, price in enumerate((100, 140, 180), 1)]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            fixture_path = folder / "cards.json"
            comps_path = folder / "comps.json"
            output_path = folder / "result.json"
            fixture_path.write_text(json.dumps(cards), encoding="utf-8")
            comps_path.write_text(json.dumps(comps), encoding="utf-8")
            result = run(None, fixture_path, settings, None, comps_path,
                         output_path, 5, 0, 0, True, 0)
        value = result["listings"][0]["valuation"]
        self.assertEqual(value["status"], "provisional")
        self.assertEqual(value["sources"], ["craigslist"])
        self.assertEqual(value["midpoint"], 140)

    def test_facebook_comparison_search_filters_to_furniture_form(self):
        target = [{"listing_id": "target", "title": "Leather sofa",
                   "search_zip": "21009"}]
        rows = [
            {"listing_id": "1", "title": "Brown leather couch", "asking_price": 100,
             "location": "Belcamp, MD", "url": "https://www.facebook.com/marketplace/item/1/"},
            {"listing_id": "2", "title": "Fabric sofa", "asking_price": 80,
             "location": "Belcamp, MD", "url": "https://www.facebook.com/marketplace/item/2/"},
        ]
        driver = unittest.mock.Mock()
        with patch("fb_marketplace.confirmed_search_place", return_value="Belcamp, Within 40 mi"), \
                patch("fb_marketplace.collect_from_page", return_value=rows):
            comps = _comp_searches(driver, target, [], 1, 5,
                                   "Belcamp, Within 40 mi",
                                   "https://www.facebook.com/marketplace/123/")
        self.assertIn("leather%20sofa", driver.get.call_args.args[0])
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0]["source"], "facebook_marketplace")
        self.assertEqual(comps[0]["search_zip"], "21009")

    def test_free_routes_combine_category_and_text_search(self):
        base = "https://www.facebook.com/marketplace/123/"
        self.assertEqual(keyword_routes(base, "free", True), [
            ("free_category", base + "free/"),
            ("free_text", base + "search/?query=free"),
        ])


if __name__ == "__main__":
    unittest.main()
