"""Scan status survives brief Windows manifest file locks."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import json

import marketplace_scan_runner as runner
from marketplace_db import known_scan_ids


class ScanManifestTests(unittest.TestCase):
    def test_scheduled_watch_checks_more_cards_without_reanalyzing_known_ones(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            calls = []

            def stage(arguments, **_kwargs):
                calls.append(arguments)
                output = Path(arguments[arguments.index("--output") + 1])
                data = ({"listings": [], "cards_checked": 36,
                         "known_cards_skipped": 36}
                        if Path(arguments[1]).name == "fb_marketplace.py" else
                        {"listings": [], "excluded_count": 0})
                output.write_text(json.dumps(data), encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with (patch.object(runner, "ROOT", root),
                  patch.object(runner, "SCAN_ROOT", root / "runs" / "scans"),
                  patch.object(runner.subprocess, "run", side_effect=stage)):
                manifest = runner.create_manifest("21009", 15, ["mower"],
                                                  mode="scheduled", account_id="alice")
                manifest["watch_item"] = {"term": "mower", "zip_code": "21009",
                                          "radius_miles": 15, "min_price": 0,
                                          "max_price": 100}
                runner.save_manifest(manifest)
                finished = runner.run_scan(manifest["id"])
            self.assertEqual(finished["status"], "complete")
            self.assertTrue(finished["collection_new_only"])
            self.assertEqual(finished["cards_checked"], 36)
            self.assertEqual(finished["known_cards_skipped"], 36)
            args = calls[0]
            self.assertEqual(args[args.index("--max-items") + 1], "120")
            self.assertEqual(args[args.index("--max-new-items") + 1], "40")
            self.assertEqual(args[args.index("--scrolls") + 1], "8")
            self.assertIn("--new-only", args)
            self.assertIn("--seen-key", args)
            self.assertEqual(len(calls), 2)

    def test_silent_depth_check_collects_ids_without_images_or_ai(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            calls = []
            probe = {"pilot_id": "coverage-test", "cards_seen": 3,
                     "depths": [{"depth": 12, "new_listing_ids": ["123"]}]}

            def stage(arguments, **_kwargs):
                calls.append(arguments)
                output = Path(arguments[arguments.index("--output") + 1])
                output.write_text(json.dumps({"listings": [{"listing_id": "123"}],
                    "cards_checked": 3, "known_cards_skipped": 2,
                    "coverage_probe": probe}), encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with (patch.object(runner, "ROOT", root),
                  patch.object(runner, "SCAN_ROOT", root / "runs" / "scans"),
                  patch.object(runner, "fast_scan_policy", return_value={
                      "max_items": 80, "scrolls": 8,
                      "pilot_id": "coverage-test", "depths": [12, 40, 80]}),
                  patch.object(runner.subprocess, "run", side_effect=stage)):
                manifest = runner.create_manifest("21009", 30, ["mower"],
                    mode="incremental", account_id="coverage:demo")
                manifest.update({"watch_item": {"term": "mower", "zip_code": "21009",
                    "radius_miles": 30, "min_price": 0, "max_price": 100},
                    "silent_pilot": True, "pilot_owner_id": "demo"})
                runner.save_manifest(manifest)
                finished = runner.run_scan(manifest["id"])
                scope = runner.seen_scope(manifest)
                known = known_scan_ids(root / runner.DEFAULT_DB, scope, ["123"])
            self.assertEqual(finished["status"], "complete")
            self.assertEqual(finished["coverage_probe"], probe)
            self.assertEqual(len(calls), 1)
            self.assertIn("--id-only", calls[0])
            self.assertEqual(known, {"123"})

    def test_fast_pilot_collects_eighty_but_passes_no_known_cards_to_ai(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            calls = []
            probe = {"pilot_id": "coverage-test", "cards_seen": 80,
                     "depths": [{"depth": depth, "new_listing_ids": []}
                                for depth in (12, 40, 80)]}

            def stage(arguments, **_kwargs):
                calls.append(arguments)
                output = Path(arguments[arguments.index("--output") + 1])
                data = ({"listings": [], "cards_checked": 80,
                         "known_cards_skipped": 80, "coverage_probe": probe}
                        if Path(arguments[1]).name == "fb_marketplace.py" else
                        {"listings": [], "excluded_count": 0})
                output.write_text(json.dumps(data), encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with (patch.object(runner, "ROOT", root),
                  patch.object(runner, "SCAN_ROOT", root / "runs" / "scans"),
                  patch.object(runner, "fast_scan_policy", return_value={
                      "max_items": 80, "scrolls": 8,
                      "pilot_id": "coverage-test", "depths": [12, 40, 80]}),
                  patch.object(runner.subprocess, "run", side_effect=stage)):
                manifest = runner.create_manifest("21009", 30, ["mower"],
                                                  mode="incremental")
                finished = runner.run_scan(manifest["id"])
            self.assertEqual(finished["status"], "complete")
            self.assertEqual(finished["coverage_probe"], probe)
            args = calls[0]
            self.assertEqual(args[args.index("--max-items") + 1], "80")
            self.assertEqual(args[args.index("--coverage-depths") + 1],
                             "12,40,80")
            self.assertEqual(len(calls), 2)

    def test_fast_collector_retries_once_then_finishes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            calls = []

            def stage(arguments, **_kwargs):
                script = Path(arguments[1]).name
                calls.append(script)
                if script == "fb_marketplace.py" and calls.count(script) == 1:
                    return SimpleNamespace(returncode=1)
                output = Path(arguments[arguments.index("--output") + 1])
                output.write_text(json.dumps({"listings": [], "cards_checked": 0,
                                              "known_cards_skipped": 0}),
                                  encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with (patch.object(runner, "ROOT", root),
                  patch.object(runner, "SCAN_ROOT", root / "runs" / "scans"),
                  patch.object(runner.subprocess, "run", side_effect=stage),
                  patch.object(runner.time, "sleep")):
                manifest = runner.create_manifest("21009", 15, ["chair"],
                                                  mode="incremental")
                finished = runner.run_scan(manifest["id"])
            self.assertEqual(finished["status"], "complete")
            self.assertEqual(calls, ["fb_marketplace.py", "fb_marketplace.py",
                                     "marketplace_radius.py"])

    def test_fast_check_imports_matching_completed_scan_history_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            watch = {"term": "mower", "zip_code": "21009", "radius_miles": 15,
                     "min_price": 0, "max_price": 100}
            with (patch.object(runner, "ROOT", root),
                  patch.object(runner, "SCAN_ROOT", root / "runs" / "scans")):
                previous = runner.create_manifest("21009", 15, ["mower"],
                                                  mode="scheduled", account_id="alice")
                previous.update({"watch_item": watch, "status": "complete"})
                runner.save_manifest(previous)
                (runner.manifest_path(previous["id"]).parent / "collection.json").write_text(
                    json.dumps({"listings": [{"listing_id": "101"},
                                              {"listing_id": "102"}]}), encoding="utf-8")
                current = runner.create_manifest("21009", 15, ["mower"],
                                                 mode="incremental", account_id="alice")
                current["watch_item"] = watch
                scope = runner.seen_scope(current)
                self.assertEqual(runner.seed_seen_scope(current, scope), 2)
                self.assertEqual(known_scan_ids(root / runner.DEFAULT_DB, scope,
                                                ["101", "102", "103"]),
                                 {"101", "102"})
                self.assertEqual(runner.seed_seen_scope(current, scope), 0)

    def test_history_seeding_does_not_hide_prior_analysis_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            watch = {"term": "mower", "zip_code": "21009", "radius_miles": 15,
                     "min_price": 0, "max_price": 100}
            with (patch.object(runner, "ROOT", root),
                  patch.object(runner, "SCAN_ROOT", root / "runs" / "scans")):
                previous = runner.create_manifest("21009", 15, ["mower"],
                                                  mode="scheduled", account_id="alice")
                previous.update({"watch_item": watch, "status": "complete"})
                runner.save_manifest(previous)
                scan_dir = runner.manifest_path(previous["id"]).parent
                (scan_dir / "nearby.json").write_text(json.dumps({
                    "excluded_listings": [], "listings": [
                        {"listing_id": "good"}, {"listing_id": "retry"}]}),
                    encoding="utf-8")
                (scan_dir / "priced.json").write_text(json.dumps({"listings": [
                    {"listing_id": "good", "vision": {"status": "observed",
                        "observations": {"listing_kind": "product"}},
                     "condition_assessment": {"status": "estimated"}},
                    {"listing_id": "retry", "vision": {"status": "error"}},
                ]}), encoding="utf-8")
                current = runner.create_manifest("21009", 15, ["mower"],
                                                 mode="incremental", account_id="alice")
                current["watch_item"] = watch
                scope = runner.seen_scope(current)
                self.assertEqual(runner.seed_seen_scope(current, scope), 1)
                self.assertEqual(known_scan_ids(root / runner.DEFAULT_DB, scope,
                                                ["good", "retry"]), {"good"})

    def test_seen_scope_changes_with_account_and_watch_filters(self):
        manifest = {"account_id": "alice", "watch_item": {
            "term": "mower", "zip_code": "21009", "radius_miles": 15,
            "min_price": 0, "max_price": 100}}
        first = runner.seen_scope(manifest)
        self.assertEqual(first, runner.seen_scope(manifest))
        self.assertEqual(first, runner.seen_scope({**manifest,
            "watch_item": {**manifest["watch_item"], "term": "MOWER",
                           "min_price": 0.0, "max_price": 100.0}}))
        self.assertNotEqual(first, runner.seen_scope({**manifest,
            "account_id": "bob"}))
        self.assertNotEqual(first, runner.seen_scope({**manifest,
            "watch_item": {**manifest["watch_item"], "radius_miles": 30}}))

    def test_fast_check_with_no_new_cards_finishes_without_ai(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            calls = []

            def stage(arguments, **_kwargs):
                script = Path(arguments[1]).name
                calls.append(arguments)
                output = Path(arguments[arguments.index("--output") + 1])
                if script == "fb_marketplace.py":
                    output.write_text(json.dumps({"listings": [], "cards_checked": 12,
                                                  "known_cards_skipped": 12}),
                                      encoding="utf-8")
                elif script == "marketplace_radius.py":
                    output.write_text(json.dumps({"listings": [], "excluded_count": 0}),
                                      encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with (patch.object(runner, "ROOT", root),
                  patch.object(runner, "SCAN_ROOT", root / "runs" / "scans"),
                  patch.object(runner.subprocess, "run", side_effect=stage)):
                manifest = runner.create_manifest("21009", 15, ["chair"],
                                                  mode="incremental")
                finished = runner.run_scan(manifest["id"])
            self.assertEqual(finished["status"], "complete")
            self.assertEqual(finished["cards_checked"], 12)
            self.assertEqual(finished["known_cards_skipped"], 12)
            self.assertEqual(finished["scanned_count"], 0)
            self.assertEqual(len(calls), 2)
            self.assertIn("--new-only", calls[0])
            self.assertEqual(calls[0][calls[0].index("--max-items") + 1], "12")

    def test_partial_analysis_keeps_failed_items_eligible_for_next_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def stage(arguments, **_kwargs):
                script = Path(arguments[1]).name
                output = Path(arguments[arguments.index("--output") + 1])
                input_file = Path(arguments[2]) if len(arguments) > 2 else None
                data = (json.loads(input_file.read_text(encoding="utf-8"))
                        if input_file and input_file.is_file() else {})
                if script == "fb_marketplace.py":
                    data = {"listings": [{"listing_id": str(i)} for i in range(1, 6)],
                            "cards_checked": 5, "known_cards_skipped": 0}
                elif script == "marketplace_radius.py":
                    data["listings"] = data["listings"][:4]
                    data["excluded_listings"] = [{"listing_id": "5"}]
                    data["excluded_count"] = 1
                elif script == "marketplace_vision.py":
                    for row in data["listings"]:
                        listing_id = row["listing_id"]
                        row["vision"] = (
                            {"status": "error"} if listing_id == "2" else
                            {"status": "filtered_offer"} if listing_id == "4" else
                            {"status": "observed", "observations": {
                                "listing_kind": "product"}})
                elif script == "marketplace_condition.py":
                    for row in data["listings"]:
                        row["condition_assessment"] = {"status": (
                            "error" if row["listing_id"] == "3" else "estimated")}
                output.write_text(json.dumps(data), encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with (patch.object(runner, "ROOT", root),
                  patch.object(runner, "SCAN_ROOT", root / "runs" / "scans"),
                  patch.object(runner.subprocess, "run", side_effect=stage)):
                manifest = runner.create_manifest("21009", 15, ["mower"],
                                                  mode="incremental", account_id="alice")
                manifest["watch_item"] = {"term": "mower", "zip_code": "21009",
                                          "radius_miles": 15, "min_price": 0,
                                          "max_price": 100}
                runner.save_manifest(manifest)
                finished = runner.run_scan(manifest["id"])
                scope = runner.seen_scope(manifest)
                known = known_scan_ids(root / runner.DEFAULT_DB, scope,
                                      [str(i) for i in range(1, 6)])
            self.assertEqual(finished["status"], "complete")
            self.assertEqual(finished["analysis_retry_pending_count"], 2)
            self.assertEqual(known, {"1", "4", "5"})

    def test_filtered_offer_does_not_hide_failed_photo_analysis(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def stage(arguments, **_kwargs):
                script = Path(arguments[1]).name
                output = Path(arguments[arguments.index("--output") + 1])
                source = Path(arguments[2]) if len(arguments) > 2 else None
                data = (json.loads(source.read_text(encoding="utf-8"))
                        if source and source.is_file() else {})
                if script == "fb_marketplace.py":
                    data = {"listings": [{"listing_id": "ad"},
                                         {"listing_id": "item"}]}
                elif script == "marketplace_vision.py":
                    data["listings"][0]["vision"] = {"status": "filtered_offer"}
                    data["listings"][1]["vision"] = {"status": "error"}
                output.write_text(json.dumps(data), encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with (patch.object(runner, "ROOT", root),
                  patch.object(runner, "SCAN_ROOT", root / "runs" / "scans"),
                  patch.object(runner.subprocess, "run", side_effect=stage)):
                manifest = runner.create_manifest("21009", 15, ["mower"])
                finished = runner.run_scan(manifest["id"])
            self.assertEqual(finished["status"], "failed")
            self.assertEqual(finished["photo_analysis"]["seller_offers_filtered"], 1)
            self.assertEqual(finished["photo_analysis"]["errors"], 1)
            self.assertIn("usable photo analysis", finished["error"])

    def test_observed_row_without_offer_type_is_not_marked_seen(self):
        filtered = {"excluded_listings": [], "listings": []}
        analyzed = {"listings": [{"listing_id": "unclear",
                                 "vision": {"status": "observed", "observations": {}},
                                 "condition_assessment": {"status": "not_applicable"}}]}
        self.assertEqual(runner.completed_listing_ids(filtered, analyzed), [])

    def test_save_retries_a_locked_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = {"id": "scan-test-21009", "status": "running"}
            actual_replace = Path.replace
            calls = []

            def briefly_locked(source, target):
                calls.append(target)
                if len(calls) < 3:
                    raise PermissionError("manifest temporarily open")
                return actual_replace(source, target)

            with (patch.object(runner, "SCAN_ROOT", Path(folder)),
                  patch.object(Path, "replace", briefly_locked),
                  patch.object(runner.time, "sleep")):
                runner.save_manifest(manifest)
                self.assertEqual(runner.read_manifest(manifest["id"])["status"],
                                 "running")
            self.assertEqual(len(calls), 3)
            self.assertEqual(list(Path(folder).rglob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
