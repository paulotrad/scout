"""Sparse price research stays bounded, visible, and variant-aware."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from marketplace_price_research import (claim_next, enqueue_scan, finish_attempt,
                                        public_queue, refresh_eligibility,
                                        recover_interrupted, _centers,
                                        _screen_center, _search_spec)


class PriceResearchTests(unittest.TestCase):
    def test_local_furniture_research_requires_single_item_and_known_condition(self):
        target = {"title": "Recliner", "description": "Needs cleaning and has scuffs.",
                  "description_status": "visible",
                  "condition_assessment": {"status": "estimated", "result": {
                      "condition_grade": "fair", "confidence": "medium"}}}
        query, variant, _ = _search_spec(target)
        self.assertEqual(query, "recliner")
        self.assertEqual(variant["product_type"], "recliner_chair")
        self.assertEqual(variant["condition_grade"], "fair")
        self.assertIsNone(_search_spec({**target, "title": "Old recliner",
                                       "description": ""})[0])
        self.assertIsNone(_search_spec({**target, "title": "Leather recliner",
                                       "description": "Stopped working"})[0])
        self.assertIsNone(_search_spec({**target,
                                       "title": "Dresser and nightstand"})[0])
        job = {"origin_zip": "21009", "variant": variant,
               "attempts": []}
        cache = {"21009": {"latitude": 39.5, "longitude": -76.3}}
        self.assertEqual(_centers(job, cache), [("21009", "Local area")])
        job["attempts"].append({"zip_code": "21009", "status": "complete"})
        self.assertEqual(_centers(job, cache), [])

    def test_furniture_research_only_saves_same_single_item_form(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = {"listing_id": "target", "title": "Recliner",
                      "description": "Needs cleaning and has scuffs.",
                      "description_status": "visible",
                      "condition_assessment": {"status": "estimated", "result": {
                          "condition_grade": "fair", "confidence": "medium"}}}
            query, variant, _ = _search_spec(target)
            rows = [{"listing_id": "peer", "title": "Recliner",
                     "asking_price": 80, "description": "Fair condition.",
                     "description_status": "visible", "location": "Bel Air, MD",
                     "radius_check": {"status": "included"},
                     "url": "https://www.facebook.com/marketplace/item/123/"},
                    {"listing_id": "other", "title": "Office chair",
                     "asking_price": 75},
                    {"listing_id": "bundle", "title": "Recliner and nightstand",
                     "asking_price": 100}]
            data = {"captured_at": "2026-10-28T12:00:00Z",
                    "requested_zip": "21009", "listings": rows}
            job = {"id": "price-test", "query": query, "variant": variant,
                   "origin_zip": "21009",
                   "targets": [{"scan_id": "scan-target", "listing_id": "target"}]}

            def collect(*args, **kwargs):
                Path(args[5]).write_text(json.dumps(data), encoding="utf-8")

            with (patch("marketplace_price_research.ROOT", root),
                  patch("marketplace_price_research.WORK_ROOT", root / "work"),
                  patch("marketplace_price_research.collect_marketplace",
                        side_effect=collect) as collector,
                  patch("marketplace_price_research.TownGeocoder"),
                  patch("marketplace_price_research.filter_result",
                        side_effect=lambda subset, *_: subset),
                  patch("marketplace_price_research.enrich_run",
                        side_effect=lambda *_args, **_kw: data),
                  patch("marketplace_price_research.build_candidates",
                        return_value={"candidate_count": 1}),
                  patch("marketplace_price_research._target_listing",
                        return_value=(root / "target.json", target))):
                self.assertEqual(_screen_center(job, "21009"), (1, 1))
                self.assertEqual(collector.call_args.args[2]["radius_miles"], 60)
                self.assertEqual(collector.call_args.kwargs["login_wait"], 20)
            approved = json.loads((root / "runs" /
                                   "price-test_21009_approved.json").read_text())
            self.assertEqual([row["listing_id"] for row in approved], ["peer"])
            self.assertEqual(approved[0]["reference_radius_miles"], 60)

    def test_named_scooter_model_is_searchable_but_generic_scooter_is_not(self):
        query, variant, _ = _search_spec({
            "title": "Razor Power Core E95 Electric Scooter. EUC"})
        self.assertEqual(query, "razor e95 electric scooter")
        self.assertEqual(variant["model"], "razor_e95")
        self.assertIsNone(_search_spec({"title": "Electric scooter"})[0])

    def test_new_matcher_promotes_existing_review_job(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "priced.json"
            queue = root / "queue.json"
            source.write_text(json.dumps({"requested_zip": "21009", "listings": [{
                "listing_id": "101", "title": "Razor E300S Seated Electric Scooter",
                "valuation": {"status": "unvalued", "comparable_count": 0}}]}),
                encoding="utf-8")
            enqueue_scan("scan-test", source, queue)
            data = json.loads(queue.read_text(encoding="utf-8"))
            data["jobs"][0]["status"] = "needs_review"
            data["jobs"][0]["query"] = None
            queue.write_text(json.dumps(data), encoding="utf-8")
            with patch("marketplace_price_research._target_listing",
                       return_value=(source, json.loads(source.read_text())[
                           "listings"][0])):
                self.assertEqual(refresh_eligibility(queue), 1)
            self.assertEqual(public_queue(queue)["jobs"][0]["status"], "queued")

    def test_interrupted_research_attempt_is_recorded_and_retryable(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = Path(folder) / "queue.json"
            queue.write_text(json.dumps({"jobs": [{
                "id": "one", "status": "running", "attempts": [{
                    "status": "running", "zip_code": "19103", "area": "Philadelphia"}],
                "current_area": "Philadelphia 19103", "note": "",
                "next_attempt_at": None}], "last_attempt_at": None}),
                encoding="utf-8")
            recover_interrupted(queue)
            job = json.loads(queue.read_text(encoding="utf-8"))["jobs"][0]
            self.assertEqual(job["status"], "waiting")
            self.assertEqual(job["attempts"][0]["status"], "failed")
            self.assertEqual(job["attempts"][0]["error"],
                             "Research worker interrupted")

    def test_sparse_variants_share_one_job_and_unclear_items_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "priced.json"
            queue = root / "queue.json"
            mower = {"title": "RYOBI ONE+ 18V 13 in. Cordless Push Lawn Mower",
                     "description": "Battery and charger included",
                     "condition": {"grade": "unknown"},
                     "valuation": {"status": "unvalued", "comparable_count": 1}}
            source.write_text(json.dumps({"requested_zip": "21009", "listings": [
                {**mower, "listing_id": "101"}, {**mower, "listing_id": "102"},
                {"listing_id": "103", "title": "Old couch",
                 "valuation": {"status": "unvalued", "comparable_count": 0}},
                {"listing_id": "104", "title": "Priced mower",
                 "valuation": {"status": "provisional", "comparable_count": 3}},
            ]}), encoding="utf-8")
            self.assertEqual(enqueue_scan("scan-test", source, queue), 2)
            self.assertEqual(enqueue_scan("scan-test", source, queue), 0)
            jobs = public_queue(queue)["jobs"]
            self.assertEqual(len(jobs), 2)
            automatic = next(job for job in jobs if job["status"] == "queued")
            self.assertEqual(automatic["price_points"], 1)
            self.assertEqual(len(automatic["targets"]), 2)
            self.assertEqual(next(job for job in jobs if job["status"] == "needs_review")[
                "title"], "Old couch")

    def test_missing_comparable_count_still_records_product_gap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "priced.json"
            queue = root / "queue.json"
            source.write_text(json.dumps({"requested_zip": "21009", "listings": [
                {"listing_id": "shed", "title": "Suncast Shed",
                 "vision": {"observations": {"listing_kind": "product"}},
                 "valuation": {"status": "unvalued",
                               "reason": "item_identity_unconfirmed"}},
                {"listing_id": "scooter", "title": "Razor E95 electric scooter",
                 "vision": {"observations": {"listing_kind": "product"}},
                 "valuation": {"status": "unvalued",
                               "reason": "item_identity_unconfirmed"}},
                {"listing_id": "service", "title": "Need shed built?",
                 "description": "We build custom sheds with delivery.",
                 "vision": {"observations": {"listing_kind": "product"}},
                 "valuation": {"status": "unvalued"}},
                {"listing_id": "pet", "title": "Puppies",
                 "vision": {"review_flags": ["pet_listing"],
                            "observations": {"listing_kind": "product"}},
                 "valuation": {"status": "unvalued", "reason": "pet_listing"}},
            ]}), encoding="utf-8")
            self.assertEqual(enqueue_scan("scan-test", source, queue), 2)
            self.assertEqual(enqueue_scan("scan-test", source, queue), 0)
            jobs = public_queue(queue)["jobs"]
            self.assertEqual(len(jobs), 2)
            self.assertEqual({job["title"] for job in jobs},
                             {"Suncast Shed", "Razor E95 electric scooter"})
            self.assertTrue(all(job["price_points"] == 0 for job in jobs))
            self.assertTrue(all(job["status"] == "needs_review" for job in jobs))
            self.assertTrue(all("Confirm the item" in job["note"] for job in jobs))
            scooter = json.loads(source.read_text(encoding="utf-8"))["listings"][1]
            with patch("marketplace_price_research._target_listing",
                       return_value=(source, scooter)):
                self.assertEqual(refresh_eligibility(queue), 0)
            self.assertTrue(all(job["status"] == "needs_review"
                                for job in public_queue(queue)["jobs"]))

    def test_area_attempts_change_regions_and_wait_between_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            queue = root / "queue.json"
            cache = root / "cache.json"
            cache.write_text(json.dumps({"21009": {"latitude": 39.5,
                                                    "longitude": -76.3}}),
                             encoding="utf-8")
            source = root / "priced.json"
            source.write_text(json.dumps({"requested_zip": "21009", "listings": [{
                "listing_id": "101", "title": "RYOBI ONE+ 18V 13 in. Cordless Push Lawn Mower",
                "description": "Battery and charger included",
                "condition": {"grade": "unknown"},
                "valuation": {"status": "unvalued", "comparable_count": 0}}]}),
                encoding="utf-8")
            enqueue_scan("scan-test", source, queue)
            centers = {"23219": ("Richmond", 37.54, -77.44),
                       "10001": ("New York", 40.75, -73.99),
                       "19103": ("Philadelphia", 39.95, -75.17)}
            now = datetime.now(timezone.utc) + timedelta(seconds=1)
            with patch("marketplace_price_research.CENTERS", centers), \
                    patch("marketplace_price_research.GEOCODE_CACHE", cache):
                first = claim_next(queue, cache, now)
                self.assertIsNotNone(first)
                self.assertEqual(first["status"], "running")
                self.assertIsNone(claim_next(queue, cache, now + timedelta(minutes=1)))
                finish_attempt(first["id"], candidate_count=1,
                               approved_count=0, price_points=0, queue_file=queue)
                waiting = public_queue(queue)["jobs"][0]
                self.assertEqual(waiting["status"], "waiting")
                self.assertIsNone(claim_next(queue, cache, now + timedelta(minutes=2)))
                later = datetime.now(timezone.utc) + timedelta(minutes=16)
                second = claim_next(queue, cache, later)
                self.assertIsNotNone(second)
                self.assertNotEqual(first["attempts"][-1]["zip_code"],
                                    second["attempts"][-1]["zip_code"])


if __name__ == "__main__":
    unittest.main()
