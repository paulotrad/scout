"""Merging ZIP scans must not count an overlapping listing twice."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from marketplace_multi_area_comps import finalize


class MultiAreaComparablesTests(unittest.TestCase):
    def test_existing_and_overlapping_listings_are_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runs = root / "runs"
            runs.mkdir()
            (runs / "old_approved.json").write_text(
                json.dumps([{"listing_id": "old"}]), encoding="utf-8")
            directory = root / "batch"
            directory.mkdir()
            details = []
            for listing_id in ("old", "shared", "new"):
                details.append({"listing_id": listing_id,
                                "description": "Runs well.",
                                "description_status": "visible"})
            (directory / "all_details.json").write_text(
                json.dumps({"listings": details}), encoding="utf-8")
            for zip_code, ids in (("17401", ("old", "shared")),
                                  ("19801", ("shared", "new"))):
                center = directory / zip_code
                center.mkdir()
                rows = [{"listing_id": listing_id,
                         "title": "Riding mower", "asking_price": 200,
                         "location": "York, PA",
                         "radius_check": {"status": "included"}}
                        for listing_id in ids]
                (center / "nearby.json").write_text(json.dumps({
                    "requested_zip": zip_code, "radius_miles_target": 60,
                    "captured_at": "2026-10-26T12:00:00+00:00",
                    "listings": rows}), encoding="utf-8")
            approved = runs / "new_approved.json"
            with patch("marketplace_multi_area_comps.ROOT", root):
                result = finalize(directory, approved, db_path=None)
                again = finalize(directory, approved, db_path=None)
            self.assertEqual(result["new_reference_count"], 2)
            self.assertEqual(again["new_reference_count"], 2)
            self.assertEqual({row["listing_id"] for row in json.loads(
                approved.read_text(encoding="utf-8"))}, {"shared", "new"})


if __name__ == "__main__":
    unittest.main()
