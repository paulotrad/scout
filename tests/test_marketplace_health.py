"""Scan health and benchmark use measured, account-scoped observations."""

from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import marketplace_web
from marketplace_health import (benchmark_csv, health_report,
                                load_benchmark, scan_summaries)


class MarketplaceHealthTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
        self.watch = {"term": "mower", "zip_code": "21009",
                      "radius_miles": 15, "min_price": 0, "max_price": 100,
                      "fast": True}
        self.account = {"configured": True, "plan": "plus_preview",
                        "watch_items": [self.watch],
                        "next_run_at": "2026-10-29T11:00:00+00:00",
                        "next_fast_run_at": "2026-10-28T18:30:00+00:00"}
        self.good = {"id": "scan-good", "watch_item": self.watch,
                     "mode": "incremental", "status": "complete",
                     "created_at": "2026-10-28T16:00:00+00:00",
                     "completed_at": "2026-10-28T16:10:00+00:00",
                     "cards_checked": 25, "scanned_count": 3,
                     "analysis_retry_pending_count": 1}
        self.failed = {"id": "scan-failed", "watch_item": self.watch,
                       "mode": "incremental", "status": "failed",
                       "created_at": "2026-10-28T17:00:00+00:00",
                       "updated_at": "2026-10-28T17:05:00+00:00",
                       "error": "Location could not be checked",
                       "cards_checked": 12, "scanned_count": 0}
        self.benchmark = {"started_at": "2026-10-28T15:00:00+00:00",
                          "ends_at": "2026-11-11T15:00:00+00:00",
                          "target_searches": 25}

    def test_health_uses_current_settings_and_actual_alert_events(self):
        old_settings = {**self.good, "id": "scan-old-settings",
                        "watch_item": {**self.watch, "radius_miles": 30},
                        "status": "complete", "cards_checked": 200}
        alerts = [{"scan_id": "scan-good", "listing_id": "123",
                   "matched_at": "2026-10-28T16:12:00+00:00"}]
        result = health_report(self.account,
                               [self.good, self.failed, old_settings], [],
                               alerts, self.benchmark,
                               [{"listing_id": "123", "vote": "down"}],
                               self.now)
        self.assertEqual(result["recent"]["attempts"], 2)
        self.assertEqual(result["recent"]["complete"], 1)
        self.assertEqual(result["recent"]["cards_checked"], 37)
        self.assertEqual(result["recent"]["new_alerts"], 1)
        self.assertEqual(result["recent"]["feedback_needs_correction"], 1)
        self.assertEqual(result["recent"]["median_scan_to_alert_minutes"], 12)
        self.assertEqual(result["watches"][0]["state"], "attention")
        self.assertEqual(result["watches"][0]["last_success_at"],
                         "2026-10-28T16:10:00+00:00")
        self.assertEqual(result["benchmark"]["enrolled_searches"], 1)
        self.assertEqual(result["benchmark"]["competitor_status"],
                         "not_measured")

    def test_queued_retry_shows_progress_without_claiming_success(self):
        queued = {"watch_item": self.watch, "status": "queued"}
        result = health_report(self.account, [self.failed], [queued], [],
                               self.benchmark, now=self.now)
        self.assertEqual(result["watches"][0]["state"], "queued")
        self.assertEqual(result["recent"]["completion_percent"], 0)
        self.assertIsNone(result["recent"]["median_scan_to_alert_minutes"])

    def test_plus_noon_check_remains_healthy_until_next_morning_slot(self):
        account = {**self.account, "watch_items": [{**self.watch, "fast": False}],
                   "next_run_at": "2026-10-29T11:00:00+00:00"}
        scan = {**self.good, "watch_item": account["watch_items"][0],
                "mode": "scheduled"}
        next_morning = datetime(2026, 10, 29, 10, 30, tzinfo=timezone.utc)
        result = health_report(account, [scan], [], [], self.benchmark,
                               now=next_morning)
        self.assertEqual(result["watches"][0]["state"], "on_track")
        overdue = datetime(2026, 10, 29, 13, 30, tzinfo=timezone.utc)
        result = health_report(account, [scan], [], [], self.benchmark,
                               now=overdue)
        self.assertEqual(result["watches"][0]["state"], "attention")

    def test_csv_contains_only_scout_measurements_in_window(self):
        csv_text = benchmark_csv([self.good, self.failed], [self.watch],
                                 self.benchmark, [])
        self.assertIn("Scout,mower,21009,15,0,100,scan-good", csv_text)
        self.assertIn("scan-failed", csv_text)
        self.assertNotIn("Dibs", csv_text)
        self.assertNotIn("FlipDar", csv_text)

    def test_benchmark_and_manifest_reads_do_not_mutate_saved_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "benchmark.json"
            config.write_text(json.dumps(self.benchmark), encoding="utf-8")
            scan_dir = root / "scan-test"
            scan_dir.mkdir()
            manifest = scan_dir / "manifest.json"
            manifest.write_text(json.dumps({"id": "scan-test", "status": "complete"}),
                                encoding="utf-8")
            self.assertEqual(load_benchmark(config), self.benchmark)
            self.assertEqual(scan_summaries(root), [{"id": "scan-test",
                                                      "status": "complete"}])
            self.assertEqual(json.loads(config.read_text(encoding="utf-8")),
                             self.benchmark)

    def test_health_routes_require_identity_and_scope_each_profile(self):
        first = {**self.account, "id": "first", "search_id": 1}
        second = {**self.account, "id": "second", "search_id": 2}
        scans = [{**self.good, "account_id": "first"},
                 {**self.failed, "account_id": "second"}]
        accounts = {"first@example.com": first, "second@example.com": second}

        def verify(token, _team, _aud):
            if not token:
                raise marketplace_web.AccessDenied("Sign in required")
            return {"email": token, "subject": "verified-user"}

        with (patch.object(marketplace_web, "PUBLIC_HOST", "scout.example.com"),
              patch.object(marketplace_web, "verify_access_token",
                           side_effect=verify),
              patch.object(marketplace_web, "get_account",
                           side_effect=lambda email, **kw: accounts[email]),
              patch.object(marketplace_web, "scan_summaries", return_value=scans),
              patch.object(marketplace_web, "scheduled_scan_owners",
                           return_value={}),
              patch.object(marketplace_web, "pending_slots", return_value=[]),
              patch.object(marketplace_web, "health_alert_events",
                           side_effect=lambda search_id, db: ([{
                               "scan_id": "scan-good", "listing_id": "123",
                               "matched_at": "2026-10-28T16:12:00+00:00"}]
                               if search_id == 1 else [])),
              patch.object(marketplace_web, "health_feedback_events",
                           return_value=[]),
              patch.object(marketplace_web, "load_benchmark",
                           return_value=self.benchmark),
              patch.object(marketplace_web, "health_report",
                           side_effect=lambda *args: health_report(
                               *args, now=self.now)),
              patch.object(marketplace_web.Handler, "log_message")):
            server = ThreadingHTTPServer(("127.0.0.1", 0), marketplace_web.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def request(path, email=None):
                    connection = HTTPConnection("127.0.0.1", server.server_port)
                    headers = {"Host": "scout.example.com"}
                    if email:
                        headers["Cf-Access-Jwt-Assertion"] = email
                    connection.request("GET", "/notify" + path, headers=headers)
                    response = connection.getresponse()
                    result = response.status, response.read().decode("utf-8")
                    connection.close()
                    return result

                self.assertEqual(request("/api/health")[0], 401)
                self.assertEqual(request("/health")[0], 401)
                first_report = json.loads(request("/api/health", "first@example.com")[1])
                second_report = json.loads(request("/api/health", "second@example.com")[1])
                self.assertEqual(first_report["recent"]["complete"], 1)
                self.assertEqual(first_report["recent"]["failed"], 0)
                self.assertEqual(first_report["recent"]["new_alerts"], 1)
                self.assertEqual(second_report["recent"]["complete"], 0)
                self.assertEqual(second_report["recent"]["failed"], 1)
                self.assertEqual(second_report["recent"]["new_alerts"], 0)
                self.assertEqual(request("/health", "first@example.com")[0], 200)
                csv_status, csv_body = request("/api/health/export.csv",
                                               "first@example.com")
                self.assertEqual(csv_status, 200)
                self.assertIn("scan-good", csv_body)
                self.assertNotIn("scan-failed", csv_body)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
