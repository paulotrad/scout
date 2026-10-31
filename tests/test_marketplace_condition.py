"""Condition guesses use the photo and seller text without inventing newness."""

import json
from pathlib import Path
import tempfile
from unittest.mock import Mock
import unittest

from marketplace_condition import classify_condition, condition_guardrails, run


class MarketplaceConditionTests(unittest.TestCase):
    def test_pet_listing_skips_condition_model(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "vision.json"
            output = Path(directory) / "conditioned.json"
            source.write_text(json.dumps({"listings": [{
                "listing_id": "pet", "title": "Dog to Good Home",
                "vision": {"status": "observed", "review_flags": ["pet_listing"],
                           "observations": {"listing_kind": "product"}},
            }]}), encoding="utf-8")
            result = run(source, output, "qwen3-vl:8b-instruct", 1, 30,
                         db_path=None)
            self.assertEqual(result["listings"][0]["condition_assessment"],
                             {"status": "not_applicable", "reason": "pet_listing"})

    def test_sends_photo_and_description_together(self):
        response = Mock()
        response.json.return_value = {"message": {"content": json.dumps({
            "usage_state": "used", "condition_grade": "needs_repair",
            "functional_status": "seller_says_needs_repair", "confidence": "medium",
            "evidence": ["Seller description: needs a new belt"],
            "uncertainties": ["Only one small photo"],
        })}}
        session = Mock()
        session.post.return_value = response
        result = classify_condition(session, b"photo", "Vacuum",
                                    "Needs a new belt and lacks attachments",
                                    {"wear_or_damage": []}, "qwen3-vl:8b-instruct", 30)
        payload = session.post.call_args.kwargs["json"]
        self.assertIn("Needs a new belt", payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][0]["images"], ["cGhvdG8="])
        self.assertEqual(result["condition_grade"], "needs_repair")

    def test_clean_photo_does_not_prove_new(self):
        assessment = {"usage_state": "new", "condition_grade": "new",
                      "functional_status": "unknown", "confidence": "high"}
        flags = condition_guardrails(assessment, "Desk", "Looks good")
        self.assertEqual(assessment["usage_state"], "unknown")
        self.assertEqual(assessment["condition_grade"], "unknown")
        self.assertIn("new_without_explicit_seller_claim", flags)

    def test_like_new_requires_seller_claim(self):
        assessment = {"usage_state": "used", "condition_grade": "like_new",
                      "functional_status": "unknown", "confidence": "high"}
        flags = condition_guardrails(assessment, "Sofa", "Looks okay")
        self.assertEqual(assessment["condition_grade"], "unknown")
        self.assertIn("like_new_without_explicit_seller_claim", flags)

    def test_never_used_is_an_explicit_seller_claim(self):
        for usage, grade in (("new", "new"), ("used", "like_new")):
            with self.subTest(usage=usage, grade=grade):
                assessment = {"usage_state": usage, "condition_grade": grade,
                              "functional_status": "unknown", "confidence": "medium"}
                flags = condition_guardrails(
                    assessment, "Mazda owner's manual",
                    "Never used Mazda CX-30 manual and pouch.")
                self.assertEqual(assessment["condition_grade"], grade)
                self.assertNotIn("new_without_explicit_seller_claim", flags)
                self.assertNotIn("like_new_without_explicit_seller_claim", flags)

    def test_explicit_broken_part_downgrades_good_and_removes_working_guess(self):
        assessment = {"usage_state": "used", "condition_grade": "good",
                      "functional_status": "unknown", "confidence": "high",
                      "evidence": ["Photo: Drawer is still functional.",
                                   "Seller description: bottom drawer track broke."]}
        flags = condition_guardrails(
            assessment, "Free dresser", "The bottom drawer track broke")
        self.assertEqual(assessment["condition_grade"], "needs_repair")
        self.assertEqual(assessment["functional_status"], "seller_says_needs_repair")
        self.assertNotIn("Photo: Drawer is still functional.", assessment["evidence"])
        self.assertIn("repair_grade_from_seller_text", flags)

    def test_vague_listing_keeps_new_used_guess_but_not_quality_grade(self):
        assessment = {"usage_state": "used", "condition_grade": "good",
                      "functional_status": "unknown", "confidence": "high",
                      "evidence": ["Photo: Air conditioner works next to couch."],
                      "uncertainties": ["A scooter is nearby."]}
        flags = condition_guardrails(
            assessment, "free couch!", "Free couch! Pick up by the curb.")
        self.assertEqual(assessment["usage_state"], "used")
        self.assertEqual(assessment["condition_grade"], "unknown")
        self.assertEqual(assessment["confidence"], "low")
        self.assertNotIn("Air conditioner", str(assessment["evidence"]))
        self.assertNotIn("scooter", str(assessment["uncertainties"]))
        self.assertIn("condition_not_supported_by_seller_text", flags)

    def test_item_conflict_requires_review_even_with_positive_seller_claim(self):
        assessment = {"usage_state": "used", "condition_grade": "good",
                      "functional_status": "unknown", "confidence": "medium"}
        flags = condition_guardrails(
            assessment, "Piano", "Very nice condition",
            ["seller_title_names_other_visible_object"])
        self.assertEqual(assessment["usage_state"], "unknown")
        self.assertEqual(assessment["condition_grade"], "unknown")
        self.assertIn("item_identity_conflict", flags)

    def test_condition_evidence_quotes_seller_without_model_claims(self):
        assessment = {"usage_state": "used", "condition_grade": "good",
                      "functional_status": "unknown", "confidence": "high",
                      "evidence": ["Photo: Unrelated appliance is like new."]}
        condition_guardrails(assessment, "Desk", "Has some marks. Pick up today.")
        self.assertEqual(assessment["evidence"][0],
                         "Seller description: Has some marks.")
        self.assertNotIn("appliance", str(assessment["evidence"]))
        self.assertEqual(assessment["confidence"], "medium")

    def test_wore_one_season_is_condition_evidence_with_four_photos(self):
        assessment = {"usage_state": "used", "condition_grade": "fair",
                      "functional_status": "unknown", "confidence": "medium"}
        flags = condition_guardrails(
            assessment, "Nike baseball cleats",
            "Wore one season. Need a good cleaning.", photo_count=4)
        self.assertEqual(assessment["condition_grade"], "fair")
        self.assertNotIn("condition_not_supported_by_seller_text", flags)
        self.assertIn("Wore one season", str(assessment["evidence"]))
        self.assertIn("4 listing photos", assessment["uncertainties"][0])

    def test_photo_model_cannot_claim_repair_from_unrelated_object(self):
        assessment = {"usage_state": "used", "condition_grade": "needs_repair",
                      "functional_status": "unknown", "confidence": "medium",
                      "evidence": ["Photo: Nearby air conditioner has loose wires."]}
        flags = condition_guardrails(assessment, "Free couch", "Pick up the couch")
        self.assertEqual(assessment["condition_grade"], "unknown")
        self.assertNotIn("air conditioner", str(assessment["evidence"]).lower())
        self.assertIn("repair_grade_not_supported_by_seller_text", flags)

    def test_fire_damaged_mower_is_parts_condition(self):
        assessment = {"usage_state": "used", "condition_grade": "unknown",
                      "functional_status": "unknown", "confidence": "low"}
        flags = condition_guardrails(
            assessment, "FREE LAWN MOWER BURNT",
            "FREE riding lawn mower. Was abused then caught fire. Good for scrap only.")
        self.assertEqual(assessment["condition_grade"], "needs_repair")
        self.assertEqual(assessment["functional_status"],
                         "seller_says_needs_repair")
        self.assertIn("repair_grade_from_seller_text", flags)

    def test_brio_cooling_failure_is_a_repair_grade(self):
        assessment = {"usage_state": "used", "condition_grade": "good",
                      "functional_status": "unknown", "confidence": "medium"}
        flags = condition_guardrails(
            assessment, "Brio Water Dispenser",
            "Hot water works. Cold water still dispenses but no longer chills. "
            "Cooling function suddenly stopped working.")
        self.assertEqual(assessment["condition_grade"], "needs_repair")
        self.assertEqual(assessment["functional_status"],
                         "seller_says_needs_repair")
        self.assertIn("repair_grade_from_seller_text", flags)

    def test_recliner_that_no_longer_reclines_is_not_good(self):
        assessment = {"usage_state": "used", "condition_grade": "good",
                      "functional_status": "seller_says_needs_repair",
                      "confidence": "medium"}
        flags = condition_guardrails(
            assessment, "Leather recliner",
            "The cushion is in great shape, but it currently does not recline.")
        self.assertEqual(assessment["condition_grade"], "needs_repair")
        self.assertIn("repair_grade_from_seller_text", flags)

    def test_spanish_parts_only_listing_is_repair_grade(self):
        assessment = {"usage_state": "used", "condition_grade": "unknown",
                      "functional_status": "unknown", "confidence": "low"}
        condition_guardrails(assessment, "Secadora", "Solo para partes")
        self.assertEqual(assessment["condition_grade"], "needs_repair")
        self.assertEqual(assessment["functional_status"],
                         "seller_says_needs_repair")

    def test_used_alone_does_not_establish_good_condition(self):
        assessment = {"usage_state": "used", "condition_grade": "good",
                      "functional_status": "unknown", "confidence": "medium"}
        flags = condition_guardrails(assessment, "Bookcase", "Free used bookcase.")
        self.assertEqual(assessment["condition_grade"], "unknown")
        self.assertIn("condition_not_supported_by_seller_text", flags)

    def test_marks_and_writing_cap_desk_at_fair(self):
        assessment = {"usage_state": "used", "condition_grade": "good",
                      "functional_status": "unknown", "confidence": "medium"}
        flags = condition_guardrails(
            assessment, "IKEA desk", "Has marks and writing on it; recommend a paint over.")
        self.assertEqual(assessment["condition_grade"], "fair")
        self.assertIn("seller_reports_moderate_wear", flags)

    def test_negated_scratch_claim_does_not_downgrade(self):
        assessment = {"usage_state": "used", "condition_grade": "good",
                      "functional_status": "unknown", "confidence": "medium"}
        condition_guardrails(assessment, "Desk", "Good condition, no scratches.")
        self.assertEqual(assessment["condition_grade"], "good")


if __name__ == "__main__":
    unittest.main()
