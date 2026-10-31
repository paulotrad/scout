"""Couch quality is cached across scans and gates condition-based pricing."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from marketplace_couch_quality import is_couch_listing, run as quality_run
from marketplace_db import get_cached_analysis
from marketplace_pricing import _condition_for_pricing, run as pricing_run
from marketplace_web import public_listing


MODEL_RESULT = {
    "photo_kind": "seller_photo", "color": "blue", "upholstery": "fabric",
    "visible_issues": [], "seller_reported_issues": ["small stain"],
    "quality_grade": "good", "confidence": "high", "photo_evidence": [],
    "seller_evidence": ["small stain"], "uncertainties": [],
}


class CouchQualityPipelineTests(unittest.TestCase):
    def test_scan_card_uses_guarded_couch_grade(self):
        item = public_listing("scan-example", {
            "listing_id": "123", "title": "Blue couch", "asking_price": 0,
            "description": "Small stain", "vision": {"observations": {
                "listing_kind": "product", "item_type": "couch"}},
            "condition_assessment": {"status": "estimated", "result": {
                "condition_grade": "good", "confidence": "medium"}},
            "couch_quality": {"status": "estimated", "model": "qwen3-vl:8b-instruct",
                "assessment": MODEL_RESULT,
                "guardrails": {"model_grade": "good", "guarded_grade": "fair",
                               "guarded_confidence": "medium", "review_required": True,
                               "color_for_matching": "blue", "reasons": ["Stain reported."]}},
        })
        self.assertEqual(item["condition_grade"], "fair")
        self.assertEqual(item["couch_quality"]["model_grade"], "good")
        self.assertIn("couch_quality_review_required", item["review_flags"])

    def test_only_physical_couches_are_selected(self):
        couch = {"title": "Furniture", "vision": {"observations": {
            "listing_kind": "product", "item_type": "couch"}}}
        service = {"title": "Couch cleaning", "vision": {"observations": {
            "listing_kind": "service", "item_type": "couch"}}}
        self.assertTrue(is_couch_listing(couch))
        self.assertFalse(is_couch_listing(service))
        self.assertTrue(is_couch_listing({"title": "Furniture", "vision": {"observations": {
            "listing_kind": "product", "item_type": "sofa_bed"}}}))

    def test_unchanged_listing_reuses_saved_8b_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "marketplace.sqlite"
            for number, description in enumerate(("Small stain", "Small stain",
                                                   "Small stain and a hole"), 1):
                scan_dir = root / f"scan-{number}"
                scan_dir.mkdir()
                (scan_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff\xdbtest-image")
                source = scan_dir / "conditioned.json"
                source.write_text(json.dumps({"listings": [{
                    "listing_id": "123", "title": "Blue couch",
                    "description": description, "image_file": "photo.jpg",
                    "vision": {"observations": {"listing_kind": "product",
                                               "item_type": "couch"}},
                }]}), encoding="utf-8")
            with patch("marketplace_couch_quality.assess", return_value=MODEL_RESULT) as model:
                first = quality_run(root / "scan-1/conditioned.json",
                                    root / "scan-1/couch_quality.json", db_path=database)
                second = quality_run(root / "scan-2/conditioned.json",
                                     root / "scan-2/couch_quality.json", db_path=database)
                third = quality_run(root / "scan-3/conditioned.json",
                                    root / "scan-3/couch_quality.json", db_path=database)
            self.assertEqual(model.call_count, 2)
            self.assertEqual(first["listings"][0]["couch_quality"]["guardrails"]["guarded_grade"], "fair")
            self.assertEqual(second["listings"][0]["couch_quality"]["status"], "estimated")
            self.assertEqual(third["listings"][0]["couch_quality"]["status"], "estimated")
            self.assertEqual(get_cached_analysis(database, "facebook_marketplace", "123")
                             ["couch_quality"]["status"], "estimated")

    def test_guarded_grade_controls_pricing_and_uncertainty_hides_rating(self):
        self.assertEqual(_condition_for_pricing({"couch_quality": {"status": "estimated",
            "guardrails": {"guarded_grade": "fair", "guarded_confidence": "medium",
                           "condition_cohort_eligible": True}}}),
            ("fair", "guarded_couch_quality"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "presented.json"
            pool = root / "pool.json"
            geocodes = root / "geocodes.json"
            source.write_text(json.dumps({"requested_zip": "21009", "listings": [{
                "listing_id": "target", "title": "Blue couch", "asking_price": 90,
                "search_zip": "21009", "vision": {"observations": {
                    "listing_kind": "product"}},
                "condition_assessment": {"status": "estimated", "result": {
                    "condition_grade": "good", "confidence": "medium"}},
                "couch_quality": {"status": "estimated", "guardrails": {
                    "guarded_grade": "unknown", "guarded_confidence": "low"}},
            }]}), encoding="utf-8")
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            pool.write_text(json.dumps([{
                "source": "facebook_marketplace", "listing_id": str(i),
                "title": "Couch", "condition": "good", "asking_price": price,
                "location": "Belcamp, MD", "observed_at": now,
                "price_reviewed": True,
            } for i, price in enumerate((80, 100, 120), 1)]), encoding="utf-8")
            geocodes.write_text(json.dumps({
                "21009": {"latitude": 39.47, "longitude": -76.30},
                "Belcamp, MD": {"latitude": 39.48, "longitude": -76.24},
            }), encoding="utf-8")
            priced = pricing_run(source, root / "priced.json", pool,
                                 db_path=None, geocode_cache=geocodes)
            valuation = priced["listings"][0]["valuation"]
            self.assertEqual(valuation["status"], "provisional")
            self.assertNotIn("price_category", valuation)
            self.assertEqual(valuation["price_category_reason"],
                             "couch_quality_requires_review")
            changed = json.loads(source.read_text(encoding="utf-8"))
            changed["listings"][0]["couch_quality"]["guardrails"] = {
                "guarded_grade": "fair", "guarded_confidence": "medium",
                "condition_cohort_eligible": True,
            }
            source.write_text(json.dumps(changed), encoding="utf-8")
            more = json.loads(pool.read_text(encoding="utf-8"))
            more.extend({
                "source": "facebook_marketplace", "listing_id": f"fair-{i}",
                "title": "Couch", "condition": "fair", "asking_price": price,
                "location": "Belcamp, MD", "observed_at": now,
                "price_reviewed": True,
            } for i, price in enumerate((40, 50, 60), 1))
            pool.write_text(json.dumps(more), encoding="utf-8")
            matched = pricing_run(source, root / "matched.json", pool,
                                  db_path=None, geocode_cache=geocodes)
            fair_value = matched["listings"][0]["valuation"]
            self.assertEqual(fair_value["condition_assumption"], "fair")
            self.assertEqual(fair_value["asking_price_sample"], [40.0, 50.0, 60.0])
            self.assertEqual(fair_value["price_category"]["code"], "high")


if __name__ == "__main__":
    unittest.main()
