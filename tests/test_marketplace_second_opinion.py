"""Second opinions must stay selective and never replace the 8B result."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import marketplace_second_opinion as second


def listing(listing_id, *, category_confidence="high", grade="unknown",
            wear=None, flags=None):
    return {
        "listing_id": listing_id, "title": f"Item {listing_id}",
        "description": "No condition stated",
        "vision": {"status": "observed", "review_flags": flags or [],
                   "observations": {"listing_kind": "product",
                       "item_type": "chair", "category_confidence": category_confidence,
                       "identity_confidence": "low",
                       "wear_or_damage": wear or []}},
        "condition_assessment": {"status": "estimated",
            "result": {"condition_grade": grade, "confidence": "low"}},
    }


class SecondOpinionTests(unittest.TestCase):
    def test_unnamed_brand_and_vague_condition_do_not_trigger_review(self):
        self.assertEqual(second.review_reasons(listing("1")), [])
        self.assertIn("item_category_uncertain", second.review_reasons(
            listing("2", category_confidence="medium")))
        self.assertIn("possible_visible_damage", second.review_reasons(
            listing("3", wear=["missing wheel"])))
        self.assertIn("identity_conflict", second.review_reasons(
            listing("4", flags=["seller_title_model_type_disagree"])))

    def test_only_highest_priority_listing_is_reviewed_and_base_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "conditioned.json"
            output = Path(folder) / "second_opinion.json"
            rows = [listing("wear", wear=["broken leg"]),
                    listing("conflict", flags=["seller_title_model_type_disagree"]),
                    listing("certain", grade="good")]
            source.write_text(json.dumps({"listings": rows}), encoding="utf-8")
            answer = {"item_type": "table", "category": "furniture",
                "condition_grade": "fair", "category_confidence": "high",
                "condition_confidence": "medium", "evidence": ["Photo: Table top"],
                "visible_damage": [], "uncertainties": []}
            with (patch.object(second, "_available", return_value=True),
                  patch.object(second, "listing_images", return_value=([b"photo"], "file")),
                  patch.object(second, "ask_model", return_value=answer) as call):
                report = second.run(source, output, limit=1, db_path=None)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(call.call_args.args[1]["listing_id"], "conflict")
            self.assertEqual(report["second_opinion_eligible"], 2)
            self.assertEqual(report["second_opinion_selected"], 1)
            self.assertEqual(report["second_opinion_reviewed"], 1)
            self.assertNotIn("second_opinion", report["listings"][0])
            reviewed = report["listings"][1]
            self.assertEqual(reviewed["vision"]["observations"]["item_type"], "chair")
            self.assertEqual(reviewed["condition_assessment"]["result"]["condition_grade"],
                             "unknown")
            self.assertTrue(reviewed["second_opinion"]["review_required"])


if __name__ == "__main__":
    unittest.main()
