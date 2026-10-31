"""Meaningful offline checks for card extraction and cautious valuation."""

import json
from contextlib import closing
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fb_marketplace import (_comp_searches, _location_queries, _us_suggestion,
                            _search_coverage, _zip_area,
                            _verify_applied_zip, close_optional_see_more_dialog,
                            depth_probe, keyword_routes,
                            load_settings, parse_card, run)
from marketplace_db import database_stats, mark_scan_seen, unchanged_scan_cards
from marketplace_value import (attach_identity_and_value, furniture_signature,
                               identify_item, infer_condition,
                               classify_asking_price)
from product_candidate import suggest_product_candidate


ROOT = Path(__file__).resolve().parents[1]


class MarketplaceTests(unittest.TestCase):
    def test_legacy_seen_cards_get_one_refresh_then_track_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "marketplace.sqlite"
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TABLE scan_seen (scope_key TEXT, listing_id TEXT, "
                           "seen_at TEXT, PRIMARY KEY (scope_key, listing_id))")
                db.execute("INSERT INTO scan_seen VALUES ('search', '101', 'yesterday')")
            card = {"listing_id": "101", "title": "Chair", "asking_price": 25}
            self.assertEqual(unchanged_scan_cards(database, "search", [card]), set())
            mark_scan_seen(database, "search", ["101"],
                           cards_by_id={"101": card})
            self.assertEqual(unchanged_scan_cards(database, "search", [card]),
                             {"101"})
            edited = {**card, "title": "Dining chair"}
            self.assertEqual(unchanged_scan_cards(database, "search", [edited]),
                             set())

    def test_optional_public_marketplace_prompt_can_be_closed(self):
        dialog = Mock()
        dialog.is_displayed.return_value = True
        dialog.text = "Join Facebook Marketplace\nGet started"
        close = Mock()
        close.is_displayed.return_value = True
        dialog.find_elements.return_value = [close]
        browser = Mock()
        browser.find_elements.return_value = [dialog]

        self.assertTrue(close_optional_see_more_dialog(browser))
        close.click.assert_called_once_with()

    def test_zip_picker_rejects_foreign_and_wrong_zip_suggestions(self):
        self.assertFalse(_us_suggestion("10001 Cáceres, Spain", "10001", "NY"))
        self.assertFalse(_us_suggestion("Manhattan, NY 03501", "10001", "NY"))
        self.assertTrue(_us_suggestion("New York, New York\nCity", "10001", "NY"))
        self.assertEqual(_location_queries("10001", "Manhattan", "NY"),
                         ["Manhattan, New York", "10001", "Manhattan",
                          "New York City", "New York City, New York"])

    def test_zip_without_locality_uses_verified_nearby_town_query(self):
        with patch("fb_marketplace.TownGeocoder") as geocoder:
            geocoder.return_value.lookup.return_value = {
                "latitude": 39.5787648, "longitude": -76.3768447,
                "display_name": "21050, Harford County, Maryland, United States"}
            geocoder.return_value.locality_for_zip.return_value = "Forest Hill"
            locality, state, _ = _zip_area("21050")
        self.assertEqual(_location_queries("21050", locality, state),
                         ["Forest Hill, Maryland", "21050", "Forest Hill"])
        self.assertTrue(_us_suggestion("Forest Hill, Maryland", "21050", "MD"))

    def test_applied_picker_location_must_be_near_requested_zip(self):
        center = (40.748, -73.994)
        with patch("fb_marketplace.TownGeocoder") as geocoder:
            geocoder.return_value.lookup.return_value = {
                "latitude": 40.713, "longitude": -74.006}
            _verify_applied_zip("New York, New York, Within 100 mi",
                                "10001", "NY", center)
            _verify_applied_zip("Belcamp, Within 20 mi",
                                "21009", "MD", center)
            with self.assertRaisesRegex(RuntimeError, "outside the state"):
                _verify_applied_zip("Cáceres, Spain, Within 100 mi",
                                    "10001", "NY", center)
            geocoder.return_value.lookup.return_value = {
                "latitude": 42.0, "longitude": -76.0}
            with self.assertRaisesRegex(RuntimeError, "too far"):
                _verify_applied_zip("Far Town, NY, Within 100 mi",
                                    "10001", "NY", center)

    def test_search_radius_covers_town_offset_from_zip(self):
        with (patch("fb_marketplace._zip_area",
                    return_value=("Abingdon", "MD", (39.4744, -76.2997))),
              patch("fb_marketplace._verify_applied_zip", return_value=3.5)):
            self.assertEqual(_search_coverage("21009", "Belcamp, Within 16 mi", 15),
                             (19, 3.5))

    def test_depth_probe_compares_unseen_ids_in_nested_prefixes(self):
        cards = [{"listing_id": str(index)} for index in range(1, 51)]
        probe = depth_probe(cards, {"1", "2", "42"}, (12, 40, 80), "pilot-1",
                            {"stop_reason": "scroll_limit",
                             "seconds_to_depth": {12: 0.5, 40: 4.2}})
        rows = probe["depths"]
        self.assertEqual([row["cards_seen"] for row in rows], [12, 40, 50])
        self.assertEqual([len(row["new_listing_ids"]) for row in rows],
                         [10, 38, 47])
        self.assertIsNone(rows[2]["seconds_to_depth"])

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

    def test_fast_collection_keeps_only_new_listing_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "cards.json"
            database = root / "marketplace.sqlite"
            settings = {"zip_code": "21009", "radius_miles": 15,
                        "keywords": ["chair"]}
            fixture.write_text(json.dumps([{
                "url": "https://www.facebook.com/marketplace/item/101/",
                "text": "$25\nOffice chair\nEdgewood, MD"}]), encoding="utf-8")
            run(None, fixture, settings, None, None, root / "first.json",
                10, 0, 0, True, 0, db_path=database)
            fixture.write_text(json.dumps([{
                "url": "https://www.facebook.com/marketplace/item/101/",
                "text": "$25\nOffice chair\nEdgewood, MD"}, {
                "url": "https://www.facebook.com/marketplace/item/102/",
                "text": "$30\nDesk chair\nEdgewood, MD"}]), encoding="utf-8")
            result = run(None, fixture, settings, None, None,
                         root / "incremental.json", 10, 0, 0, True, 0,
                         db_path=database, new_only=True)
            self.assertEqual(result["cards_checked"], 2)
            self.assertEqual(result["known_cards_skipped"], 1)
            self.assertEqual([row["listing_id"] for row in result["listings"]], ["102"])

    def test_fast_collection_tracks_seen_cards_per_account_search(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "cards.json"
            database = root / "marketplace.sqlite"
            settings = {"zip_code": "21009", "radius_miles": 15,
                        "keywords": ["chair"]}
            fixture.write_text(json.dumps([{
                "url": "https://www.facebook.com/marketplace/item/101/",
                "text": "$25\nOffice chair\nEdgewood, MD"}]), encoding="utf-8")
            run(None, fixture, settings, None, None, root / "global.json",
                10, 0, 0, True, 0, db_path=database)
            first = run(None, fixture, settings, None, None, root / "account-a.json",
                        10, 0, 0, True, 0, db_path=database, new_only=True,
                        seen_key="account-a-chair")
            self.assertEqual(first["listing_count"], 1)
            mark_scan_seen(database, "account-a-chair", ["101"],
                           cards_by_id={"101": first["listings"][0]})
            repeat = run(None, fixture, settings, None, None, root / "repeat.json",
                         10, 0, 0, True, 0, db_path=database, new_only=True,
                         seen_key="account-a-chair")
            other = run(None, fixture, settings, None, None, root / "account-b.json",
                        10, 0, 0, True, 0, db_path=database, new_only=True,
                        seen_key="account-b-chair")
            self.assertEqual(repeat["listing_count"], 0)
            self.assertEqual(repeat["known_cards_skipped"], 1)
            self.assertEqual(other["listing_count"], 1)
            fixture.write_text(json.dumps([{
                "url": "https://www.facebook.com/marketplace/item/101/",
                "text": "$15\nOffice chair\nEdgewood, MD"}]), encoding="utf-8")
            edited = run(None, fixture, settings, None, None, root / "edited.json",
                         10, 0, 0, True, 0, db_path=database, new_only=True,
                         seen_key="account-a-chair")
            self.assertEqual(edited["listing_count"], 1)
            self.assertEqual(edited["rechecked_cards"], 1)
            self.assertEqual(edited["known_cards_skipped"], 0)
            mark_scan_seen(database, "account-a-chair", ["101"],
                           cards_by_id={"101": edited["listings"][0]})
            unchanged = run(None, fixture, settings, None, None,
                            root / "unchanged.json", 10, 0, 0, True, 0,
                            db_path=database, new_only=True,
                            seen_key="account-a-chair")
            self.assertEqual(unchanged["listing_count"], 0)

    def test_new_card_budget_does_not_count_known_cards_or_lose_backlog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "cards.json"
            database = root / "marketplace.sqlite"
            fixture.write_text(json.dumps([{
                "url": f"https://www.facebook.com/marketplace/item/{index}/",
                "text": f"$25\nMower {index}\nEdgewood, MD"}
                for index in range(1, 7)]), encoding="utf-8")
            mark_scan_seen(database, "mower-search", ["1", "2"],
                           cards_by_id={str(index): {"title": f"Mower {index}",
                                                     "asking_price": 25}
                                        for index in (1, 2)})
            settings = {"zip_code": "21009", "radius_miles": 15,
                        "keywords": ["mower"]}
            first = run(None, fixture, settings, None, None, root / "first.json",
                        10, 0, 0, True, 0, db_path=database, new_only=True,
                        seen_key="mower-search", max_new_items=2)
            self.assertEqual(first["cards_checked"], 6)
            self.assertEqual(first["known_cards_skipped"], 2)
            self.assertEqual(first["new_cards_deferred"], 2)
            self.assertEqual([row["listing_id"] for row in first["listings"]],
                             ["3", "4"])
            mark_scan_seen(database, "mower-search", ["3", "4"],
                           cards_by_id={str(row["listing_id"]): row
                                        for row in first["listings"]})
            second = run(None, fixture, settings, None, None, root / "second.json",
                         10, 0, 0, True, 0, db_path=database, new_only=True,
                         seen_key="mower-search", max_new_items=2)
            self.assertEqual([row["listing_id"] for row in second["listings"]],
                             ["5", "6"])
            self.assertEqual(second["known_cards_skipped"], 4)
            self.assertEqual(second["new_cards_deferred"], 0)

    def test_pilot_fixture_records_deeper_ids_but_reviews_only_unseen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "cards.json"
            database = root / "marketplace.sqlite"
            fixture.write_text(json.dumps([{
                "url": f"https://www.facebook.com/marketplace/item/{index}/",
                "text": f"$25\nMower {index}\nEdgewood, MD"}
                for index in range(1, 16)]), encoding="utf-8")
            mark_scan_seen(database, "pilot-scope", [str(index)
                                                       for index in range(1, 11)],
                           cards_by_id={str(index): {"title": f"Mower {index}",
                                                     "asking_price": 25}
                                        for index in range(1, 11)})
            result = run(None, fixture, {"zip_code": "21009", "radius_miles": 30,
                         "keywords": ["mower"]}, None, None,
                         root / "pilot.json", 80, 8, 0, True, 0,
                         db_path=database, new_only=True, seen_key="pilot-scope",
                         coverage_depths=(12, 40, 80),
                         coverage_pilot_id="coverage-test")
            self.assertEqual(result["cards_checked"], 15)
            self.assertEqual(result["listing_count"], 5)
            self.assertEqual([len(row["new_listing_ids"]) for row in
                              result["coverage_probe"]["depths"]], [2, 5, 5])

    def test_product_candidate_marks_broad_types_without_inventing_a_model(self):
        desk = suggest_product_candidate("Solid wood desk")
        self.assertEqual(desk["types"][0]["type"], "desk")
        self.assertEqual(desk["material_clues"], ["solid_wood"])
        self.assertFalse(desk["needs_review"])
        bundle = suggest_product_candidate("Free piano and China cabinet")
        self.assertEqual(len(bundle["types"]), 2)
        self.assertTrue(bundle["needs_review"])
        self.assertEqual(suggest_product_candidate("Free")["types"], [])

    def test_id_only_fixture_keeps_card_ids_without_images_or_valuation(self):
        cards = [{"url": "https://www.facebook.com/marketplace/item/123/",
                  "text": "$75\nRyobi electric mower\nEdgewood, MD",
                  "image_url": "https://example.com/mower.jpg"}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "cards.json"
            fixture.write_text(json.dumps(cards), encoding="utf-8")
            output = root / "pilot.json"
            with patch("fb_marketplace.store_result") as stored:
                result = run(None, fixture, {"zip_code": "21009",
                    "radius_miles": 30, "keywords": ["mower"]}, None, None,
                    output, 80, 8, 0, True, 0, db_path=root / "listing.sqlite",
                    coverage_depths=(12, 40, 80),
                    coverage_pilot_id="coverage-test", id_only=True)
            stored.assert_not_called()
            self.assertEqual(result["listing_count"], 1)
            self.assertEqual(result["identified_count"], 0)
            self.assertEqual(result["images_downloaded"], 0)
            self.assertNotIn("_image_url", result["listings"][0])
            self.assertEqual(result["coverage_probe"]["depths"][0]["new_listing_ids"],
                             ["123"])

    def test_id_only_live_collection_has_zero_image_downloads(self):
        driver = unittest.mock.Mock()
        driver.current_url = "https://www.facebook.com/marketplace/123/"
        cards = [{"listing_id": "123", "title": "Mower",
                  "asking_price": 75, "_image_url": "https://example.com/photo.jpg"}]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pilot.json"
            with (patch("fb_marketplace.create_driver",
                        return_value=(driver, lambda: None)),
                  patch("fb_marketplace.close_optional_see_more_dialog"),
                  patch("fb_marketplace.set_location_zip",
                        return_value="Edgewood, Within 30 mi"),
                  patch("fb_marketplace.confirmed_search_place",
                        return_value="Edgewood, Within 34 mi"),
                  patch("fb_marketplace._search_coverage", return_value=(34, 3.5)),
                  patch("fb_marketplace.collect_from_page", return_value=cards),
                  patch("fb_marketplace.save_listing_images") as download):
                result = run(None, None, {"zip_code": "21009",
                    "radius_miles": 30, "keywords": ["mower"]}, None, None,
                    output, 80, 8, 0, True, 0,
                    coverage_depths=(12, 40, 80),
                    coverage_pilot_id="coverage-test", id_only=True)
            download.assert_not_called()
            self.assertEqual(result["images_downloaded"], 0)
            self.assertEqual(result["cards_checked"], 1)
            self.assertEqual(result["radius_miles_applied"], 34)
            self.assertEqual(result["search_center_offset_miles"], 3.5)
            driver.get.assert_any_call(
                "https://www.facebook.com/marketplace/123/search/?query=mower&radius=55")

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

    def test_five_asking_price_categories_handle_flat_samples(self):
        sample = [80, 100, 120, 140, 160]
        self.assertEqual([classify_asking_price(price, sample)["code"]
                          for price in (70, 100, 120, 135, 200)],
                         ["best", "good", "average", "slightly_above", "high"])
        self.assertEqual(classify_asking_price(100, [100, 100, 100])["code"],
                         "average")
        self.assertEqual(classify_asking_price(0, sample)["code"], "best")
        self.assertIsNone(classify_asking_price(100, sample[:2]))

    def test_supplied_craigslist_comps_flow_through_offline_run(self):
        settings = load_settings(ROOT / "marketplace_settings.example.json")
        cards = [{"url": "https://www.facebook.com/marketplace/item/999/",
                  "text": "$0\nLeather couch\nEdgewood, MD"}]
        comps = [{"source": "craigslist", "listing_id": str(index),
                  "title": "Leather sofa", "asking_price": price,
                  "search_zip": "21009",
                  "observed_at": datetime.now(timezone.utc).isoformat()}
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

    def test_search_routes_send_radius_in_kilometres(self):
        base = "https://www.facebook.com/marketplace/nyc/"
        self.assertEqual(keyword_routes(base, "Ryobi 13 mower", False, 100), [
            ("Ryobi 13 mower", base + "search/?query=Ryobi%2013%20mower&radius=161")])
        self.assertEqual(keyword_routes(base, "free", True, 15), [
            ("free_category", base + "free/?radius=25"),
            ("free_text", base + "search/?query=free&radius=25")])


if __name__ == "__main__":
    unittest.main()
