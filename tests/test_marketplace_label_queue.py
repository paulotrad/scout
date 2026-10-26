"""The human review sheet keeps seller text and proposed labels separate."""

import csv
import json
from pathlib import Path
import tempfile
import unittest

from marketplace_label_queue import export_queue


class LabelQueueTests(unittest.TestCase):
    def test_exports_description_and_unverified_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "sample.json"
            output = base / "review.csv"
            source.write_text(json.dumps({"listings": [{
                "listing_id": "123", "title": "Free", "description": "Wood desk",
                "refactored_description": "Seller describes a wood desk.",
                "condition_assessment": {"result": {
                    "usage_state": "used", "condition_grade": "good",
                    "confidence": "medium", "functional_status": "unknown",
                    "evidence": ["Seller description: good condition"],
                    "uncertainties": [],
                }},
                "vision": {"model": "qwen3.5:2b", "observations": {
                    "category": "furniture", "item_type": "desk",
                    "category_confidence": "high",
                    "candidate_identity": None, "identity_confidence": "low",
                    "classification_evidence": ["Photo: desk", "Seller description: wood desk"],
                }},
            }]}), encoding="utf-8")
            self.assertEqual(export_queue(source, output), 1)
            with output.open(encoding="utf-8-sig", newline="") as file:
                row = next(csv.DictReader(file))
            self.assertEqual(row["description"], "Wood desk")
            self.assertEqual(row["refactored_description"],
                             "Seller describes a wood desk.")
            self.assertEqual(list(row)[-1], "refactored_description")
            self.assertEqual(row["vision_item_type"], "desk")
            self.assertEqual(row["condition_usage_state"], "used")
            self.assertEqual(row["condition_grade"], "good")
            self.assertIn("Seller description:", row["vision_classification_evidence"])
            self.assertEqual(row["verified_item_type"], "")
            self.assertEqual(row["verified_identity"], "")


if __name__ == "__main__":
    unittest.main()
