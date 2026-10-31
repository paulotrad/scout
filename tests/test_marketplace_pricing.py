"""Reviewed asking prices feed cautious finished-scan estimates."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from marketplace_pricing import _condition_for_pricing, _price_scopes, _recent_price, run


RECENT = datetime.now(timezone.utc).isoformat()


class ReviewedPricingTests(unittest.TestCase):
    def test_same_battery_mower_variant_widens_until_three_matches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "presented.json"
            pool = root / "pool.json"
            cache = root / "geocodes.json"
            title = "Ryobi ONE+ 18V 13 in cordless push mower"
            source.write_text(json.dumps({"requested_zip": "21009",
                "captured_at": RECENT, "listings": [{
                    "listing_id": "target", "title": title,
                    "description": "Includes battery and charger",
                    "asking_price": 75, "search_zip": "21009",
                    "condition": {"grade": "good"}}]}), encoding="utf-8")
            rows = [{"source": "facebook_marketplace", "listing_id": str(index),
                     "title": title, "description": "Includes battery and charger",
                     "reference_type": "push_mower", "asking_price": price,
                     "condition": "good", "search_zip": "21009",
                     "location": place, "observed_at": RECENT,
                     "price_reviewed": True}
                    for index, (price, place) in enumerate(((60, "Near, MD"),
                                                              (80, "Mid, PA"),
                                                              (100, "Far, PA")), 1)]
            rows.append({**rows[0], "listing_id": "gas", "title": "Gas push mower",
                         "description": "Pull start", "asking_price": 20})
            pool.write_text(json.dumps(rows), encoding="utf-8")
            cache.write_text(json.dumps({
                "21009": {"latitude": 39.0, "longitude": -76.0},
                "Near, MD": {"latitude": 39.1, "longitude": -76.0},
                "Mid, PA": {"latitude": 40.2, "longitude": -76.0},
                "Far, PA": {"latitude": 40.6, "longitude": -76.0}}),
                encoding="utf-8")
            result = run(source, root / "priced.json", pool, db_path=None,
                         geocode_cache=cache)
        value = result["listings"][0]["valuation"]
        self.assertEqual(value["reference_scope_miles"], 120)
        self.assertEqual(value["reference_scope_label"], "regional")
        self.assertEqual(value["comparable_count"], 3)
        self.assertEqual(value["midpoint"], 80)
        self.assertEqual(value["price_category"]["confidence"], "low")
        self.assertEqual(_price_scopes({"title": "Used couch"}), (30, 60))

    def test_reprice_older_scan_with_newer_references_records_reference_time(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "presented.json"
            pool = directory / "pool.json"
            cache = directory / "geocodes.json"
            captured = datetime.fromisoformat(RECENT) - timedelta(hours=2)
            source.write_text(json.dumps({"requested_zip": "21009",
                "captured_at": captured.isoformat(), "listings": [{
                    "listing_id": "target", "title": "Push mower",
                    "search_zip": "21009", "condition": {"grade": "good"}}]}),
                encoding="utf-8")
            pool.write_text(json.dumps([{
                "source": "facebook_marketplace", "listing_id": str(index),
                "title": "Push mower", "reference_type": "push_mower",
                "asking_price": price, "condition": "good", "search_zip": "21009",
                "location": "Belcamp, MD", "observed_at": RECENT,
                "price_reviewed": True,
            } for index, price in enumerate((40, 70, 100), 1)]), encoding="utf-8")
            cache.write_text(json.dumps({
                "21009": {"latitude": 39.47, "longitude": -76.30},
                "Belcamp, MD": {"latitude": 39.48, "longitude": -76.24}}),
                encoding="utf-8")
            before = run(source, directory / "before.json", pool,
                         db_path=None, geocode_cache=cache)
            after = run(source, directory / "after.json", pool,
                        db_path=None, geocode_cache=cache,
                        reference_time=datetime.fromisoformat(RECENT))
        self.assertEqual(before["reviewed_pricing"]["status"],
                         "no_prices_in_last_year")
        self.assertEqual(after["listings"][0]["valuation"]["reason"],
                         "mower_power_source_unclear")
        self.assertEqual(after["listings"][0]["valuation"]["reference_scope_miles"], 30)
        self.assertEqual(after["reviewed_pricing"]["reference_as_of"], RECENT)

    def test_seller_reported_nonfunction_outweighs_image_condition(self):
        listing = {"title": "Lawn mower",
                   "description": "I can’t get it to start",
                   "condition_assessment": {"status": "estimated", "result": {
                       "condition_grade": "good", "confidence": "high"}}}
        self.assertEqual(_condition_for_pricing(listing),
                         ("parts", "seller_reports_repair_or_nonfunction"))

    def test_insufficient_general_comparable_count_replaces_stale_result(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "presented.json"
            pool = directory / "pool.json"
            cache = directory / "geocodes.json"
            source.write_text(json.dumps({"requested_zip": "21009", "listings": [{
                "listing_id": "target", "title": "Lawn mower",
                "search_zip": "21009", "identity": {"id": None},
                "valuation": {"status": "unvalued", "comparable_count": 0},
            }]}), encoding="utf-8")
            pool.write_text(json.dumps([{
                "source": "facebook_marketplace", "listing_id": "comp",
                "title": "Lawn mower", "reference_type": "lawn_mower",
                "asking_price": 100, "condition": "good", "search_zip": "21009",
                "location": "Belcamp, MD", "observed_at": RECENT,
                "price_reviewed": True,
            }]), encoding="utf-8")
            cache.write_text(json.dumps({
                "21009": {"latitude": 39.47, "longitude": -76.30},
                "Belcamp, MD": {"latitude": 39.48, "longitude": -76.24}}),
                encoding="utf-8")
            result = run(source, directory / "priced.json", pool,
                         db_path=None, geocode_cache=cache)
        self.assertEqual(result["listings"][0]["valuation"]["comparable_count"], 0)
        self.assertEqual(result["listings"][0]["valuation"]["reason"],
                         "mower_power_source_unclear")
        self.assertEqual(result["listings"][0]["valuation"]["needed"], 3)

    def test_named_scooter_model_uses_median_asking_price_of_same_model(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "presented.json"
            pool = directory / "pool.json"
            cache = directory / "geocodes.json"
            source.write_text(json.dumps({"requested_zip": "21009", "listings": [{
                "listing_id": "target", "title": "Razor E95 Electric scooter",
                "search_zip": "21009", "asking_price": 70,
                "condition": {"grade": "unknown"}, "identity": {"id": None},
                "vision": {"observations": {"listing_kind": "product"}},
            }]}), encoding="utf-8")
            comparables = [{"source": "facebook_marketplace", "listing_id": str(index),
                            "title": "Razor E95 Electric scooter", "reference_type": "electric_scooter",
                            "asking_price": price, "condition": "good",
                            "search_zip": "21009", "location": "Belcamp, MD",
                            "observed_at": RECENT, "price_reviewed": True}
                           for index, price in enumerate((80, 100, 120), 1)]
            comparables.append({"source": "facebook_marketplace", "listing_id": "mower",
                                "title": "Riding mower", "reference_type": "riding_mower",
                                "asking_price": 10, "condition": "good",
                                "search_zip": "21009", "location": "Belcamp, MD",
                                "observed_at": RECENT, "price_reviewed": True})
            pool.write_text(json.dumps(comparables), encoding="utf-8")
            cache.write_text(json.dumps({
                "21009": {"latitude": 39.47, "longitude": -76.30},
                "Belcamp, MD": {"latitude": 39.48, "longitude": -76.24}}),
                encoding="utf-8")
            result = run(source, directory / "priced.json", pool,
                         db_path=None, geocode_cache=cache)
        value = result["listings"][0]["valuation"]
        self.assertEqual(value["status"], "provisional")
        self.assertEqual(value["midpoint"], 100)
        self.assertEqual(value["comparable_count"], 3)
        self.assertEqual(value["product_type"], "electric_scooter")

    def test_repricing_removes_old_rating_for_generic_scooter(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "prior.json"
            pool = directory / "pool.json"
            cache = directory / "geocodes.json"
            source.write_text(json.dumps({"requested_zip": "21009", "listings": [{
                "listing_id": "target", "title": "Electric scooter",
                "asking_price": 960, "valuation": {"status": "provisional",
                    "price_category": {"code": "high", "label": "High price"}},
            }]}), encoding="utf-8")
            pool.write_text(json.dumps([{
                "source": "facebook_marketplace", "listing_id": str(index),
                "title": "Razor E95 Electric scooter",
                "reference_type": "electric_scooter",
                "asking_price": price, "condition": "good",
                "search_zip": "21009", "location": "Belcamp, MD",
                "observed_at": RECENT, "price_reviewed": True}
                for index, price in enumerate((60, 80, 100), 1)]),
                encoding="utf-8")
            cache.write_text(json.dumps({
                "21009": {"latitude": 39.47, "longitude": -76.30},
                "Belcamp, MD": {"latitude": 39.48, "longitude": -76.24}}),
                encoding="utf-8")
            result = run(source, directory / "repriced.json", pool,
                         db_path=None, geocode_cache=cache)
        value = result["listings"][0]["valuation"]
        self.assertEqual(value["status"], "unvalued")
        self.assertEqual(value["reason"], "scooter_variant_details_missing")
        self.assertNotIn("price_category", value)

    def test_only_dated_prices_from_the_previous_365_days_count(self):
        now = datetime.fromisoformat(RECENT)
        self.assertTrue(_recent_price({"observed_at": RECENT}, now))
        self.assertFalse(_recent_price({}, now))
        self.assertFalse(_recent_price({"observed_at": (
            now - timedelta(days=366)).isoformat()}, now))
        self.assertFalse(_recent_price({"observed_at": (
            now + timedelta(days=1)).isoformat()}, now))

    def test_reviewed_pool_estimates_nearby_zip_couch_with_model_condition(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "presented.json"
            output = directory / "priced.json"
            pool = directory / "pool.json"
            cache = directory / "geocodes.json"
            cache.write_text(json.dumps({
                "21050": {"latitude": 39.47, "longitude": -76.30},
                "Belcamp, MD": {"latitude": 39.48, "longitude": -76.24}}),
                encoding="utf-8")
            source.write_text(json.dumps({"requested_zip": "21050", "listings": [
                {"listing_id": "target", "title": "Free couch", "asking_price": 100,
                 "search_zip": "21050",
                 "identity": {"id": None}, "condition": {"grade": "unknown"},
                 "condition_assessment": {"status": "estimated", "result": {
                     "condition_grade": "good", "confidence": "medium"}},
                 "vision": {"observations": {"listing_kind": "product"}}},
                {"listing_id": "ad", "title": "Couch pickup service",
                 "search_zip": "21050", "vision": {"observations": {
                     "listing_kind": "service"}}}]}
            ), encoding="utf-8")
            pool.write_text(json.dumps([{
                "source": "facebook_marketplace", "listing_id": str(index),
                "title": "Couch", "asking_price": price,
                "condition": "good", "search_zip": "21009",
                "location": "Belcamp, MD",
                "source_url": f"https://www.facebook.com/marketplace/item/{index}/",
                "observed_at": RECENT, "price_reviewed": True,
            } for index, price in enumerate((80, 100, 120), 1)]), encoding="utf-8")
            result = run(source, output, pool, db_path=None, geocode_cache=cache)
            self.assertTrue(output.is_file())
        estimate = result["listings"][0]["valuation"]
        self.assertEqual(estimate["status"], "provisional")
        self.assertEqual(estimate["midpoint"], 100)
        self.assertEqual(estimate["comparable_count"], 3)
        self.assertEqual(estimate["condition_source"],
                         "guarded_image_and_seller_assessment")
        self.assertEqual(estimate["reference_scope_miles"], 30)
        self.assertEqual(estimate["price_category"]["code"], "average")
        self.assertEqual(len(estimate["comparable_evidence"]), 3)
        self.assertEqual(estimate["comparable_evidence"][0]["asking_price"], 80)
        self.assertEqual(estimate["comparable_evidence"][0]["source_url"],
                         "https://www.facebook.com/marketplace/item/1/")
        self.assertEqual(len(estimate["price_category"]["cutoffs"]), 4)
        self.assertEqual(result["reviewed_pricing"]["estimated_items"], 1)
        self.assertNotIn("valuation", result["listings"][1])

    def test_distant_zip_does_not_borrow_21009_prices(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "presented.json"
            pool = directory / "pool.json"
            cache = directory / "geocodes.json"
            source.write_text(json.dumps({"requested_zip": "10001", "listings": [
                {"listing_id": "target", "title": "Couch",
                 "search_zip": "10001", "valuation": {"status": "unvalued"}}]}), encoding="utf-8")
            pool.write_text(json.dumps([{
                "source": "facebook_marketplace", "listing_id": "1",
                "title": "Couch", "asking_price": 100,
                "condition": "good", "search_zip": "21009",
                "location": "Belcamp, MD", "observed_at": RECENT,
                "price_reviewed": True}]), encoding="utf-8")
            cache.write_text(json.dumps({
                "10001": {"latitude": 40.75, "longitude": -73.99},
                "Belcamp, MD": {"latitude": 39.48, "longitude": -76.24}}),
                encoding="utf-8")
            result = run(source, directory / "priced.json",
                         pool, db_path=None, geocode_cache=cache)
        self.assertEqual(result["reviewed_pricing"]["status"], "applied")
        self.assertEqual(result["reviewed_pricing"]["nearby_pool_size"], 0)
        self.assertEqual(result["listings"][0]["valuation"]["status"], "unvalued")

    def test_widens_price_reference_only_when_local_sample_is_too_small(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "presented.json"
            pool = directory / "pool.json"
            cache = directory / "geocodes.json"
            source.write_text(json.dumps({"requested_zip": "21050", "listings": [
                {"listing_id": "target", "title": "Used couch",
                 "search_zip": "21050", "condition": {"grade": "good"}}]}),
                encoding="utf-8")
            pool.write_text(json.dumps([{
                "source": "facebook_marketplace", "listing_id": str(index),
                "title": "Couch", "asking_price": price, "condition": "good",
                "search_zip": "21009", "location": "Far Town, MD",
                "observed_at": RECENT, "price_reviewed": True,
            } for index, price in enumerate((100, 120, 140), 1)]), encoding="utf-8")
            cache.write_text(json.dumps({
                "21050": {"latitude": 39.5, "longitude": -76.3},
                "Far Town, MD": {"latitude": 39.5, "longitude": -75.5}}),
                encoding="utf-8")
            result = run(source, directory / "priced.json", pool,
                         db_path=None, geocode_cache=cache)
        self.assertEqual(result["listings"][0]["valuation"]["reference_scope_miles"], 60)
        self.assertEqual(result["listings"][0]["valuation"]["comparable_count"], 3)

    def test_conflicting_card_and_seller_prices_do_not_get_a_category(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "presented.json"
            pool = directory / "pool.json"
            cache = directory / "geocodes.json"
            source.write_text(json.dumps({"requested_zip": "21009", "listings": [
                {"listing_id": "target", "title": "Couch", "search_zip": "21009",
                 "asking_price": 300, "description": "Asking $600."}]}), encoding="utf-8")
            pool.write_text(json.dumps([{
                "source": "facebook_marketplace", "listing_id": str(index),
                "title": "Couch", "asking_price": price,
                "condition": "good", "search_zip": "21009",
                "location": "Belcamp, MD", "observed_at": RECENT,
                "price_reviewed": True,
            } for index, price in enumerate((80, 100, 120), 1)]), encoding="utf-8")
            cache.write_text(json.dumps({
                "21009": {"latitude": 39.47, "longitude": -76.30},
                "Belcamp, MD": {"latitude": 39.48, "longitude": -76.24}}),
                encoding="utf-8")
            result = run(source, directory / "priced.json", pool,
                         db_path=None, geocode_cache=cache)
        value = result["listings"][0]["valuation"]
        self.assertEqual(value["status"], "provisional")
        self.assertNotIn("price_category", value)
        self.assertEqual(value["price_category_reason"],
                         "card_and_description_price_conflict")

    def test_prices_older_than_one_year_are_not_used(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "presented.json"
            pool = directory / "pool.json"
            cache = directory / "geocodes.json"
            source.write_text(json.dumps({"requested_zip": "21009",
                "captured_at": RECENT, "listings": [{"listing_id": "target",
                "title": "Couch", "search_zip": "21009"}]}), encoding="utf-8")
            old = (datetime.fromisoformat(RECENT) - timedelta(days=366)).isoformat()
            pool.write_text(json.dumps([{
                "source": "facebook_marketplace", "listing_id": str(index),
                "title": "Couch", "asking_price": price,
                "condition": "good", "search_zip": "21009",
                "location": "Belcamp, MD", "observed_at": old,
                "price_reviewed": True,
            } for index, price in enumerate((80, 100, 120), 1)]), encoding="utf-8")
            cache.write_text(json.dumps({
                "21009": {"latitude": 39.47, "longitude": -76.30},
                "Belcamp, MD": {"latitude": 39.48, "longitude": -76.24}}),
                encoding="utf-8")
            result = run(source, directory / "priced.json", pool,
                         db_path=None, geocode_cache=cache)
        self.assertEqual(result["reviewed_pricing"]["status"],
                         "no_prices_in_last_year")
        self.assertEqual(result["reviewed_pricing"]["older_or_future_prices_excluded"], 3)
        self.assertNotIn("valuation", result["listings"][0])


    def test_unknown_comparable_conditions_keep_rating_low_confidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output = root / "presented.json", root / "priced.json"
            pool, places = root / "pool.json", root / "geocodes.json"
            source.write_text(json.dumps({"requested_zip": "21009", "listings": [{
                "listing_id": "target", "title": "Used couch", "asking_price": 50,
                "condition_assessment": {"status": "estimated", "result": {
                    "condition_grade": "good", "confidence": "high"}},
                "vision": {"observations": {"listing_kind": "product"}},
            }]}), encoding="utf-8")
            pool.write_text(json.dumps([{
                "source": "facebook_marketplace", "listing_id": str(index),
                "title": "Couch", "asking_price": 80 + index * 10,
                "condition": "unknown", "search_zip": "21009",
                "location": "Edgewood, MD", "observed_at": RECENT,
                "price_reviewed": True,
            } for index in range(8)]), encoding="utf-8")
            places.write_text(json.dumps({
                "21009": {"latitude": 39.43, "longitude": -76.30},
                "Edgewood, MD": {"latitude": 39.44, "longitude": -76.29},
            }), encoding="utf-8")
            item = run(source, output, pool, db_path=None,
                       geocode_cache=places)["listings"][0]
        self.assertEqual(item["valuation"]["unknown_comparable_conditions"], 8)
        self.assertEqual(item["valuation"]["price_category"]["confidence"], "low")


if __name__ == "__main__":
    unittest.main()
