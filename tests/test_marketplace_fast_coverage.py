"""The fast-check depth pilot compares one collection and chooses a bounded cap."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from marketplace_fast_coverage import (
    fast_scan_policy, finish_pilot_if_due, public_pilot_status, start_pilot,
    stop_pilot,
)


class FastCoverageTests(unittest.TestCase):
    def test_one_day_pilot_is_idempotent_and_scoped_to_one_search(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pilot.json"
            scans = Path(folder) / "scans"
            now = datetime(2026, 10, 27, 16, tzinfo=timezone.utc)
            item = {"term": "mower", "zip_code": "21009", "radius_miles": 30,
                    "min_price": 0, "max_price": 100}
            first = start_pilot("demo", item, now=now, path=path)
            second = start_pilot("demo", item, now=now + timedelta(hours=2),
                                 path=path)
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(first["ends_at"], second["ends_at"])
            manifest = {"mode": "incremental", "account_id": "demo",
                        "watch_item": item}
            active = fast_scan_policy(manifest, now=now, path=path,
                                      scan_root=scans)
            self.assertEqual((active["max_items"], active["scrolls"]), (80, 8))
            self.assertEqual(active["depths"], [12, 40, 80])
            self.assertEqual(fast_scan_policy({**manifest,
                "watch_item": {**item, "max_price": 200}}, now=now,
                path=path, scan_root=scans)["max_items"], 12)
            self.assertIsNone(public_pilot_status("other", path=path))
            self.assertTrue(public_pilot_status("demo", watch_items=[],
                                                path=path)["paused"])
            self.assertFalse(public_pilot_status("demo", watch_items=[
                {**item, "fast": True}], path=path)["paused"])

    def test_report_picks_smallest_depth_covering_95_percent_of_new_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "pilot.json"
            scans = root / "scans"
            now = datetime(2026, 10, 27, 16, tzinfo=timezone.utc)
            item = {"term": "mower", "zip_code": "21009", "radius_miles": 30,
                    "min_price": 0, "max_price": 100}
            pilot = start_pilot("demo", item, now=now, path=path, silent=True)
            for index in range(4):
                directory = scans / f"scan-{index}"
                directory.mkdir(parents=True)
                probe = {"pilot_id": pilot["id"], "depths": [
                    {"depth": 12, "new_listing_ids": [f"early-{index}"],
                     "seconds_to_depth": 0.1},
                    {"depth": 40, "new_listing_ids": [f"early-{index}",
                                                      f"deep-{index}"],
                     "seconds_to_depth": 4.0},
                    {"depth": 80, "new_listing_ids": [f"early-{index}",
                                                      f"deep-{index}"],
                     "seconds_to_depth": 9.0}]}
                (directory / "manifest.json").write_text(json.dumps({
                    "status": "complete", "coverage_probe": probe}),
                    encoding="utf-8")
            failure = scans / "scan-failed"
            failure.mkdir()
            (failure / "manifest.json").write_text(json.dumps({
                "status": "failed", "coverage_pilot_id": pilot["id"]}),
                encoding="utf-8")
            finished = finish_pilot_if_due(now=now + timedelta(hours=24),
                                           path=path, scan_root=scans)
            self.assertEqual(finished["status"], "complete")
            self.assertEqual(finished["report"]["runs"], 5)
            self.assertEqual(finished["report"]["completed_runs"], 4)
            self.assertEqual(finished["report"]["chosen_depth"], 40)
            self.assertEqual([row["unique_new_ids"] for row in
                              finished["report"]["depths"]], [4, 8, 8])
            self.assertEqual(fast_scan_policy({"mode": "incremental",
                "account_id": "demo", "watch_item": item},
                now=now + timedelta(hours=25), path=path,
                scan_root=scans)["max_items"], 40)

    def test_few_checks_keep_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "pilot.json"
            scans = root / "scans"
            now = datetime(2026, 10, 27, 16, tzinfo=timezone.utc)
            start_pilot("demo", {"term": "mower", "zip_code": "21009",
                "radius_miles": 30, "min_price": 0, "max_price": 100},
                now=now, path=path)
            result = finish_pilot_if_due(now=now + timedelta(hours=24),
                                         path=path, scan_root=scans)
            self.assertEqual(result["report"]["confidence"], "insufficient_data")
            self.assertEqual(result["report"]["chosen_depth"], 12)

    def test_silent_pilot_only_changes_its_own_scan_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            item = {"term": "mower", "zip_code": "21009", "radius_miles": 30,
                    "min_price": 0, "max_price": 100}
            pilot = start_pilot("demo", item, path=root / "pilot.json",
                                silent=True)
            owner_scan = {"mode": "incremental", "account_id": "demo",
                          "watch_item": item}
            study_scan = {**owner_scan, "account_id": "coverage:demo",
                          "pilot_owner_id": "demo", "silent_pilot": True}
            self.assertEqual(fast_scan_policy(owner_scan, path=root / "pilot.json",
                                              scan_root=root / "scans")["max_items"], 12)
            self.assertEqual(fast_scan_policy(study_scan, path=root / "pilot.json",
                                              scan_root=root / "scans")["max_items"], 80)
            status = public_pilot_status("demo", watch_items=[],
                                         path=root / "pilot.json",
                                         scan_root=root / "scans")
            self.assertFalse(status["paused"])
            self.assertTrue(status["silent"])
            self.assertEqual(status["term"], "mower")
            self.assertEqual(pilot["status"], "running")

    def test_stopped_pilot_does_not_resume_or_change_the_watchlist(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pilot.json"
            item = {"term": "mower", "zip_code": "21009", "radius_miles": 30,
                    "min_price": 0, "max_price": 100}
            start_pilot("demo", item, path=path, silent=True)
            stopped = stop_pilot("Gather broader price references", path=path)
            self.assertEqual(stopped["status"], "cancelled")
            self.assertEqual(stopped["watch_item"], item)
            self.assertEqual(finish_pilot_if_due(path=path)["status"], "cancelled")
            self.assertEqual(public_pilot_status("demo", path=path)["stop_reason"],
                             "Gather broader price references")


if __name__ == "__main__":
    unittest.main()
