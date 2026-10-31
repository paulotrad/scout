"""The couch pilot must distinguish defect evidence from model confidence."""

import json
from pathlib import Path
import unittest

from marketplace_couch_quality_rules import apply_quality_guardrails


class CouchQualityGuardrailTests(unittest.TestCase):
    def test_saved_pilot_flags_overgraded_listings(self):
        path = Path(__file__).resolve().parents[1] / "runs/couch_quality_8b_pilot.json"
        rows = {str(row["listing_id"]): row for row in json.loads(path.read_text(encoding="utf-8"))["results"]}
        expected = {
            "1131410439376587": "good",       # A few dog snags
            "2085505362114903": "fair",       # Detached back cushion
            "1990677861574387": "poor",       # Sleeper springs can be felt
            "1066748662498720": "unknown",    # Catalog photo, new claim
            "4419235261683202": "fair",       # Hole in cushion
            "954635620989234": "fair",        # Catalog photo, paint stain disclosed
            "2985254918474052": "unknown",    # No concrete condition evidence
            "2043986256489526": "unknown",    # Vague 'nice couch'
        }
        for listing_id, grade in expected.items():
            row = rows[listing_id]
            with self.subTest(listing_id=listing_id):
                result = apply_quality_guardrails(row["assessment"], row["title"], row["description"])
                self.assertEqual(result["guarded_grade"], grade)
                self.assertEqual(result["condition_cohort_eligible"],
                                 grade in {"good", "fair"} and
                                 result["guarded_confidence"] in {"medium", "high"} and
                                 row["assessment"]["photo_kind"] == "seller_photo")

    def test_negated_defects_do_not_trigger_penalty(self):
        result = apply_quality_guardrails(
            {"quality_grade": "good", "confidence": "high", "photo_kind": "seller_photo",
             "photo_evidence": ["Actual couch has no stains or tears."],
             "visible_issues": [], "color": "blue"},
            "Blue sofa", "Excellent condition. No holes, scratches or stains.",
        )
        self.assertEqual(result["guarded_grade"], "good")
        self.assertEqual(result["color_for_matching"], "blue")

    def test_model_paraphrase_is_not_independent_seller_evidence(self):
        result = apply_quality_guardrails(
            {"quality_grade": "good", "confidence": "high", "photo_kind": "seller_photo",
             "seller_reported_issues": ["large tear"], "photo_evidence": []},
            "Sofa", "Nice couch",
        )
        self.assertEqual(result["guarded_grade"], "unknown")
        self.assertEqual(result["guarded_confidence"], "low")

    def test_catalog_photo_cannot_establish_new_or_color(self):
        result = apply_quality_guardrails(
            {"quality_grade": "new", "confidence": "high", "photo_kind": "catalog_or_graphic",
             "color": "gray", "photo_evidence": ["Staged showroom couch"]},
            "Brand new sofa", "New in box. Wrong color shipped.",
        )
        self.assertEqual(result["guarded_grade"], "unknown")
        self.assertEqual(result["color_for_matching"], "unknown")
        self.assertTrue(result["review_required"])


if __name__ == "__main__":
    unittest.main()
