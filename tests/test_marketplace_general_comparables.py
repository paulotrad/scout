"""Admin price samples combine areas without doubling overlapping listings."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import marketplace_web


class GeneralComparablePageTests(unittest.TestCase):
    def test_price_samples_include_new_mower_areas_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runs = root / "runs"
            runs.mkdir()
            general = runs / "general.json"
            general.write_text(json.dumps({
                "requested_zip": "21009", "observed_at": "2026-10-26T12:00:00Z",
                "listings": [{"listing_id": "1", "reference_type": "push_mower",
                              "effective_ask": 50, "review_status": "review"}]}),
                encoding="utf-8")
            center = runs / "mower_comps_multi_20261026" / "17401"
            center.mkdir(parents=True)
            (center / "candidates.json").write_text(json.dumps({
                "requested_zip": "17401", "observed_at": "2026-10-27T02:00:00Z",
                "listings": [{"listing_id": "1", "reference_type": "push_mower",
                              "effective_ask": 50, "review_status": "candidate"},
                             {"listing_id": "2", "reference_type": "riding_mower",
                              "effective_ask": 300, "review_status": "candidate"}]}),
                encoding="utf-8")
            (runs / "test_approved.json").write_text(json.dumps([
                {"listing_id": "1"}, {"listing_id": "2"}]), encoding="utf-8")
            with patch.object(marketplace_web, "ROOT", root), patch.object(
                    marketplace_web, "GENERAL_CANDIDATES", general):
                data = marketplace_web.general_comparables()
        self.assertEqual(len(data["listings"]), 2)
        self.assertEqual(next(row for row in data["listings"]
                              if row["listing_id"] == "1")["review_status"],
                         "candidate")
        self.assertEqual(data["summary"]["approved_count"], 2)
        self.assertEqual(data["summary"]["requested_zips"], ["17401", "21009"])
        self.assertEqual(data["summary"]["by_type"]["riding_mower"]["median_asking_price"],
                         300)


if __name__ == "__main__":
    unittest.main()
