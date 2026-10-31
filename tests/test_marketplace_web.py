"""The local scan site validates requests and keeps untrusted data bounded."""

import unittest
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import ANY, patch

from marketplace_scan_runner import validate_settings
from marketplace_alerts import (
    get_account, grant_admin, hide_all_inbox_alerts as marketplace_web_hide_all,
    is_admin_email, list_admin_emails, list_alerts,
    open_account, record_scan_matches, save_account, set_alert_triage,
)
from marketplace_email import email_status, finish_email_scan, prepare_email_batch
import marketplace_web
from marketplace_web import public_listing, public_scan, result_path, scan_stage_progress


class MarketplaceWebTests(unittest.TestCase):
    def test_used_candidate_route_uses_saved_item_and_scan_area(self):
        manifest = {"id": "scan-example", "zip_code": "21009",
                    "radius_miles": 45}
        listing = {"id": "123", "used_research": {"query": "Ryobi mower"}}
        with (patch.object(marketplace_web, "read_manifest", return_value=manifest),
              patch.object(marketplace_web, "public_scan",
                           return_value={"listings": [listing]}),
              patch.object(marketplace_web, "search_craigslist",
                           return_value={"candidates": []}) as search,
              patch.object(marketplace_web.Handler, "log_message")):
            server = ThreadingHTTPServer(("127.0.0.1", 0), marketplace_web.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def request(listing_id):
                    connection = HTTPConnection("127.0.0.1", server.server_port)
                    connection.request("GET", f"/api/scans/scan-example/used-candidates/{listing_id}")
                    response = connection.getresponse()
                    status = response.status
                    response.read()
                    connection.close()
                    return status
                self.assertEqual(request("123"), 200)
                search.assert_called_once_with("Ryobi mower", "21009", 45)
                self.assertEqual(request("999"), 404)
                self.assertEqual(search.call_count, 1)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_quality_overview_counts_distinct_recent_items_and_corrections(self):
        scans = [{"id": "scan-one", "status": "complete",
                  "created_at": "2026-10-28T12:00:00Z"},
                 {"id": "scan-two", "status": "complete",
                  "created_at": "2026-10-28T11:00:00Z"}]
        item = {"id": "123", "display_title": "Nike baseball cleats",
                "condition_grade": "unknown", "condition_reason": "Needs review",
                "valuation": {"status": "unvalued",
                              "reason": "price_group_unavailable"}}
        correction = {"account_id": "alice", "scan_id": "scan-one",
                      "listing_id": "123", "vote": "down",
                      "issue": "wrong_condition", "corrected_condition": "fair"}
        with (patch.object(marketplace_web, "public_scan",
                           return_value={"listings": [item]}),
              patch.object(marketplace_web, "corrections_to_review",
                           return_value=[correction]),
              patch.object(marketplace_web, "correction_count", return_value=1)):
            result = marketplace_web.quality_overview(scans)
        self.assertEqual(result["item_count"], 1)
        self.assertEqual(result["unknown_condition_count"], 1)
        self.assertEqual(result["unpriced_count"], 1)
        self.assertEqual(result["corrections"][0]["title"],
                         "Nike baseball cleats")

    def test_alert_list_adds_saved_photo_price_evidence_and_direct_link(self):
        listing = {"id": "123456789", "title": "Ryobi mower",
                   "asking_price": 75, "card_asking_price": 75,
                   "price_source": "facebook_card", "location": "Edgewood, MD",
                   "distance_miles_approx": 3.5,
                   "url": "https://www.facebook.com/marketplace/item/123456789/",
                   "valuation": {"low": 50, "high": 110,
                                 "price_category": {"label": "Average price",
                                                    "comparison_count": 13}},
                   "image_url": "/api/scans/scan-one/images/123456789"}
        payload = {"unread_count": 1, "alerts": [
            {"id": 1, "scan_id": "scan-one", "listing_id": "123456789"},
            {"id": 2, "scan_id": "scan-missing", "listing_id": "555555555"}]}
        def read(scan_id):
            if scan_id == "scan-missing":
                raise FileNotFoundError(scan_id)
            return {"id": scan_id, "keywords": ["mower"]}
        with (patch.object(marketplace_web, "read_manifest", side_effect=read),
              patch.object(marketplace_web, "public_scan",
                           return_value={"listings": [listing]})):
            result = marketplace_web.actionable_alerts(payload)
        self.assertEqual(result["unread_count"], 1)
        self.assertEqual(result["alerts"][0]["id"], 1)
        self.assertEqual(result["alerts"][0]["price"], 75)
        self.assertIn("$50–$110", result["alerts"][0]["comparison_label"])
        self.assertEqual(result["alerts"][0]["image_url"], listing["image_url"])
        self.assertEqual(result["alerts"][0]["facebook_url"], listing["url"])
        self.assertEqual(result["alerts"][1], payload["alerts"][1])

    def test_silent_mower_pilot_uses_separate_seen_scope_and_no_notifications(self):
        item = {"term": "mower", "zip_code": "21009", "radius_miles": 30,
                "min_price": 0, "max_price": 100}
        account = {"id": "demo", "plan": "plus_preview",
                   "watch_items": [{"term": "free", "zip_code": "21009",
                                    "radius_miles": 45, "min_price": 0,
                                    "max_price": 0, "fast": True}]}
        pilot = {"id": "coverage-test", "status": "running", "silent": True,
                 "account_id": "demo", "watch_item": item}
        slot = "pilot:coverage-test:fast-2026-10-27-13-00:item:hash"
        pending = {"account_id": "demo", "slot_key": slot,
                   "watch_item": item, "scan_mode": "coverage_pilot"}
        with (patch.object(marketplace_web, "pending_slots", return_value=[pending]),
              patch.object(marketplace_web, "scan_manifests", return_value=[]),
              patch.object(marketplace_web, "active_scan_id", None),
              patch.object(marketplace_web, "load_pilot", return_value=pilot),
              patch.object(marketplace_web, "start_scan",
                           return_value={"id": "scan-pilot"}) as start,
              patch.object(marketplace_web, "finish_slot") as finish):
            self.assertEqual(marketplace_web.start_next_pending({"demo": account}),
                             "scan-pilot")
        self.assertEqual(start.call_args.args[0]["mode"], "incremental")
        self.assertEqual(start.call_args.kwargs["account_id"], "coverage:demo")
        self.assertTrue(start.call_args.kwargs["silent_pilot"])
        self.assertEqual(start.call_args.kwargs["pilot_owner_id"], "demo")
        finish.assert_called_once_with(slot, "scan-pilot", account_id="demo")
        with (patch.object(marketplace_web, "record_scan_matches") as record,
              patch.object(marketplace_web, "finish_email_scan") as finish_email):
            marketplace_web.complete_scan_notifications(
                {"id": "scan-pilot", "status": "complete", "silent_pilot": True})
        record.assert_not_called()
        finish_email.assert_not_called()

    def test_silent_pilot_queues_hourly_without_changing_saved_free_search(self):
        free = {"term": "free", "zip_code": "21009", "radius_miles": 45,
                "min_price": 0, "max_price": 0, "fast": True}
        mower = {"term": "mower", "zip_code": "21009", "radius_miles": 30,
                 "min_price": 0, "max_price": 100}
        account = {"id": "demo", "configured": True, "plan": "plus_preview",
                   "timezone": "America/New_York", "watch_items": [free]}
        now = datetime(2026, 10, 27, 17, 5, tzinfo=timezone.utc)
        with (patch.object(marketplace_web, "list_account_profiles",
                           return_value=[account]),
              patch.object(marketplace_web, "pending_slots", return_value=[]),
              patch.object(marketplace_web, "due_slot", return_value=None),
              patch.object(marketplace_web, "load_pilot", return_value={
                  "id": "coverage-test", "status": "running", "silent": True,
                  "account_id": "demo", "watch_item": mower}),
              patch.object(marketplace_web, "claim_slot", return_value=True) as claim,
              patch.object(marketplace_web, "start_next_pending", return_value=None)):
            marketplace_web.run_due_schedule(now)
        self.assertEqual(account["watch_items"], [free])
        self.assertEqual(claim.call_count, 2)
        self.assertEqual(claim.call_args_list[0].kwargs["watch_item"], free)
        self.assertEqual(claim.call_args_list[1].kwargs["watch_item"], mower)
        self.assertEqual(claim.call_args_list[1].kwargs["scan_mode"],
                         "coverage_pilot")

    def test_activity_uses_current_keyword_settings_and_reports_retry(self):
        item = {"term": "Mower", "zip_code": "21009", "radius_miles": 30,
                "min_price": 0, "max_price": 100, "fast": True}
        account = {"watch_items": [item]}
        scans = [
            {"id": "scan-failed", "mode": "incremental", "status": "failed",
             "watch_item": item, "created_at": "2026-10-27T10:30:00-04:00",
             "error": "Collection failed"},
            {"id": "scan-done", "mode": "incremental", "status": "complete",
             "watch_item": item, "created_at": "2026-10-27T10:00:00-04:00",
             "completed_at": "2026-10-27T10:01:00-04:00",
             "cards_checked": 12, "scanned_count": 2},
            {"id": "scan-old-settings", "mode": "scheduled", "status": "complete",
             "watch_item": {**item, "max_price": 200},
             "created_at": "2026-10-27T11:00:00-04:00"},
        ]
        queue = [{"status": "queued", "id": None, "watch_item": item,
                  "retry_attempt": 1, "created_at": "2026-10-27T10:31:00-04:00"}]
        activity = marketplace_web.account_scan_activity(account, scans, queue)
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["status"], "queued")
        self.assertEqual(activity[0]["retry_attempt"], 1)
        self.assertEqual(activity[0]["last_attempt_at"],
                         "2026-10-27T10:30:00-04:00")
        self.assertEqual(activity[0]["last_success_at"],
                         "2026-10-27T10:01:00-04:00")
        self.assertEqual(activity[0]["error"], "Collection failed")

    def test_restart_marks_scheduled_scan_failed_for_retry(self):
        scheduled = {"id": "scan-scheduled", "status": "running",
                     "mode": "incremental", "account_id": "buyer",
                     "watch_item": {"term": "mower"}}
        manual = {"id": "scan-manual", "status": "running", "mode": "manual"}
        with (patch.object(marketplace_web, "scan_manifests",
                           return_value=[scheduled, manual]),
              patch.object(marketplace_web, "save_manifest") as save,
              patch.object(marketplace_web, "complete_scan_notifications") as notify,
              patch.dict("os.environ", {"SCOUT_RESUME_SCAN_ID": ""})):
            marketplace_web.mark_interrupted_scans()
        self.assertEqual(scheduled["status"], "failed")
        self.assertEqual(manual["status"], "interrupted")
        self.assertEqual(save.call_count, 2)
        notify.assert_called_once_with(scheduled)

    def test_failed_fast_scan_queues_only_one_recent_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scan_dir = root / "scan-test-failure"
            scan_dir.mkdir()
            item = {"term": "mower", "zip_code": "21009", "radius_miles": 15,
                    "min_price": 0, "max_price": 100, "fast": True}
            now = datetime(2026, 10, 23, 14, 30, tzinfo=timezone.utc)
            failed = {"id": scan_dir.name, "status": "failed",
                      "mode": "incremental", "account_id": "buyer",
                      "watch_item": item,
                      "updated_at": (now - timedelta(minutes=3)).isoformat()}
            (scan_dir / "manifest.json").write_text(json.dumps(failed),
                                                     encoding="utf-8")
            account = {"id": "buyer", "plan": "plus_preview",
                       "watch_items": [item]}
            with (patch.object(marketplace_web, "SCAN_ROOT", root),
                  patch.object(marketplace_web, "list_account_profiles",
                               return_value=[account]),
                  patch.object(marketplace_web, "claim_slot", return_value=True) as claim,
                  patch.object(marketplace_web, "save_manifest") as save):
                self.assertEqual(marketplace_web.queue_failed_scan_retries(now), 1)
            self.assertTrue(claim.call_args.args[0].startswith(
                "retry:scan-test-failure:item:"))
            self.assertEqual(claim.call_args.kwargs["scan_mode"], "incremental")
            self.assertEqual(claim.call_args.kwargs["retry_attempt"], 1)
            save.assert_called_once()
            failed["retry_attempt"] = 1
            (scan_dir / "manifest.json").write_text(json.dumps(failed),
                                                     encoding="utf-8")
            with (patch.object(marketplace_web, "SCAN_ROOT", root),
                  patch.object(marketplace_web, "list_account_profiles",
                               return_value=[account]),
                  patch.object(marketplace_web, "claim_slot") as claim):
                self.assertEqual(marketplace_web.queue_failed_scan_retries(now), 0)
            claim.assert_not_called()
            failed["retry_attempt"] = 0
            (scan_dir / "manifest.json").write_text(json.dumps(failed),
                                                     encoding="utf-8")
            with (patch.object(marketplace_web, "SCAN_ROOT", root),
                  patch.object(marketplace_web, "list_account_profiles",
                               return_value=[{**account, "watch_items": [
                                   {**item, "term": "shed"}]}]),
                  patch.object(marketplace_web, "claim_slot") as claim):
                self.assertEqual(marketplace_web.queue_failed_scan_retries(now), 0)
            claim.assert_not_called()

    def test_pending_retry_waits_and_reuses_current_priority_settings(self):
        item = {"term": "mower", "zip_code": "21009", "radius_miles": 15,
                "min_price": 0, "max_price": 100, "fast": True}
        account = {"id": "buyer", "plan": "plus_preview",
                   "watch_items": [{**item, "radius_miles": 30}]}
        retry = {"account_id": "buyer", "slot_key": "retry:scan-old:item:hash",
                 "watch_item": item, "scan_mode": "incremental",
                 "retry_root": "scan-old", "retry_attempt": 1,
                 "not_before": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
        with (patch.object(marketplace_web, "pending_slots", return_value=[retry]),
              patch.object(marketplace_web, "scan_manifests", return_value=[]),
              patch.object(marketplace_web, "active_scan_id", None),
              patch.object(marketplace_web, "start_scan") as start):
            self.assertIsNone(marketplace_web.start_next_pending({"buyer": account}))
        start.assert_not_called()
        retry["not_before"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with (patch.object(marketplace_web, "pending_slots", return_value=[retry]),
              patch.object(marketplace_web, "scan_manifests", return_value=[]),
              patch.object(marketplace_web, "active_scan_id", None),
              patch.object(marketplace_web, "start_scan",
                           return_value={"id": "scan-retry"}) as start,
              patch.object(marketplace_web, "finish_slot") as finish):
            self.assertEqual(marketplace_web.start_next_pending({"buyer": account}),
                             "scan-retry")
        self.assertEqual(start.call_args.args[0]["radius_miles"], 30)
        self.assertEqual(start.call_args.args[0]["mode"], "incremental")
        self.assertEqual(start.call_args.kwargs["email_batch_size"], 1)
        self.assertEqual(start.call_args.kwargs["retry_attempt"], 1)
        finish.assert_called_once()

    def test_removed_keyword_retry_is_released_before_waiting_for_retry_time(self):
        old = {"term": "electric scooter", "zip_code": "21009",
               "radius_miles": 30, "min_price": 0, "max_price": 30}
        current = {**old, "term": "shed", "max_price": 100}
        pending = {"account_id": "buyer", "slot_key": "retry:old:item:scooter",
                   "watch_item": old, "scan_mode": "scheduled",
                   "not_before": (datetime.now(timezone.utc) +
                                  timedelta(days=1)).isoformat()}
        with (patch.object(marketplace_web, "pending_slots", return_value=[pending]),
              patch.object(marketplace_web, "scan_manifests", return_value=[]),
              patch.object(marketplace_web, "active_scan_id", None),
              patch.object(marketplace_web, "release_slot") as release,
              patch.object(marketplace_web, "start_scan") as start):
            self.assertIsNone(marketplace_web.start_next_pending({
                "buyer": {"id": "buyer", "watch_items": [current]}}))
        release.assert_called_once_with(pending["slot_key"], account_id="buyer")
        start.assert_not_called()

    def test_removed_keyword_retry_does_not_block_current_fast_scan(self):
        old = {"term": "electric scooter", "zip_code": "21009",
               "radius_miles": 30, "min_price": 0, "max_price": 30}
        current = {**old, "term": "shed", "fast": True}
        account = {"id": "buyer", "plan": "plus_preview",
                   "watch_items": [current]}
        pending = {"account_id": "buyer", "slot_key": "retry:old:item:scooter",
                   "watch_item": old}
        with (patch.object(marketplace_web, "list_account_profiles",
                           return_value=[account]),
              patch.object(marketplace_web, "pending_slots", return_value=[pending]),
              patch.object(marketplace_web, "due_slot", return_value=None),
              patch.object(marketplace_web, "due_fast_slot", return_value="fast-now"),
              patch.object(marketplace_web, "claim_slot") as claim,
              patch.object(marketplace_web, "load_pilot", return_value=None),
              patch.object(marketplace_web, "start_next_pending", return_value=None)):
            marketplace_web.run_due_schedule()
        self.assertEqual(claim.call_args.kwargs["watch_item"], current)

    def test_temporary_scan_start_failure_keeps_scheduled_slot(self):
        item = {"term": "mower", "zip_code": "21009", "radius_miles": 15,
                "min_price": 0, "max_price": 100}
        pending = {"account_id": "buyer", "slot_key": "today:item:mower",
                   "watch_item": item}
        account = {"id": "buyer", "watch_items": [item], "plan": "free"}
        with (patch.object(marketplace_web, "pending_slots",
                           return_value=[pending]),
              patch.object(marketplace_web, "scan_manifests", return_value=[]),
              patch.object(marketplace_web, "active_scan_id", None),
              patch.object(marketplace_web, "start_scan",
                           side_effect=OSError("temporary disk error")),
              patch.object(marketplace_web, "release_slot") as release):
            with self.assertRaises(OSError):
                marketplace_web.start_next_pending({"buyer": account})
        release.assert_not_called()

    def test_pending_slot_links_existing_manifest_after_worker_crash(self):
        item = {"term": "mower", "zip_code": "21009", "radius_miles": 15,
                "min_price": 0, "max_price": 100}
        slot = "2026-10-23-07:item:hash"
        pending = [{"account_id": "buyer", "slot_key": slot,
                    "watch_item": item}]
        existing = [{"id": "scan-existing", "account_id": "buyer",
                     "scheduled_slot_key": slot, "status": "running"}]
        with (patch.object(marketplace_web, "pending_slots", return_value=pending),
              patch.object(marketplace_web, "scan_manifests", return_value=existing),
              patch.object(marketplace_web, "active_scan_id", None),
              patch.object(marketplace_web, "start_scan") as start,
              patch.object(marketplace_web, "finish_slot") as finish):
            self.assertIsNone(marketplace_web.start_next_pending({"buyer": {"id": "buyer"}}))
        start.assert_not_called()
        finish.assert_called_once_with(slot, "scan-existing", account_id="buyer")

    def test_stopped_scan_with_finished_pricing_is_recovered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scan_id = "scan-recover-21009"
            scan_dir = root / "runs" / "scans" / scan_id
            scan_dir.mkdir(parents=True)
            (scan_dir / "priced.json").write_text(json.dumps({"listings": []}),
                                                  encoding="utf-8")
            stale = {"id": scan_id, "status": "running",
                     "updated_at": (datetime.now(timezone.utc) -
                                    timedelta(minutes=3)).isoformat()}
            with (patch.object(marketplace_web, "ROOT", root),
                  patch.object(marketplace_web, "SCAN_ROOT", scan_dir.parent),
                  patch.object(marketplace_web, "scan_manifests", return_value=[stale]),
                  patch.object(marketplace_web, "active_scan_id", None),
                  patch.object(marketplace_web, "save_manifest") as saved,
                  patch.object(marketplace_web, "complete_scan_notifications") as notified):
                marketplace_web.reconcile_orphaned_scans()
            self.assertEqual(stale["status"], "complete")
            self.assertTrue((scan_dir / "review.csv").is_file())
            self.assertEqual(stale["result_file"],
                             str((scan_dir / "priced.json").relative_to(root)))
            saved.assert_called_once()
            notified.assert_called_once()

    def test_public_admin_requires_verified_email_with_admin_role(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            couch_file = Path(directory) / "couches.json"
            couch_file.write_text(json.dumps({"listings": [{"listing_id": "123"}]}),
                                  encoding="utf-8")
            general_file = Path(directory) / "general.json"
            general_file.write_text(json.dumps({"listings": [{"listing_id": "456"}]}),
                                    encoding="utf-8")
            grant_admin("admin@example.com", db)
            open_account("member@example.com", db)

            def verified(token, _team, _aud):
                if not token:
                    raise marketplace_web.AccessDenied("Cloudflare Access login is required")
                return {"email": token, "subject": "verified-user"}

            with (patch.object(marketplace_web, "PUBLIC_HOST", "scout.example.com"),
                  patch.object(marketplace_web, "PUBLIC_WRITES", True),
                  patch.object(marketplace_web, "verify_access_token",
                               side_effect=verified),
                  patch.object(marketplace_web, "is_admin_email",
                               side_effect=lambda email: is_admin_email(email, db)),
                  patch.object(marketplace_web, "list_admin_emails",
                               side_effect=lambda: list_admin_emails(db)),
                  patch.object(marketplace_web, "get_account",
                               side_effect=lambda email=None, **kw: get_account(email, db, **kw)),
                  patch.object(marketplace_web, "admin_overview",
                               return_value={"accounts": [], "unassigned_scans": []}),
                  patch.object(marketplace_web, "couch_comparables",
                               return_value={"summary": {}, "listings": [{"listing_id": "123"}]}),
                  patch.object(marketplace_web, "general_comparables",
                               return_value={"summary": {}, "listings": [{"listing_id": "456"}]}),
                  patch.object(marketplace_web, "COUCH_DETAILS", couch_file),
                  patch.object(marketplace_web, "GENERAL_CANDIDATES", general_file),
                  patch.object(marketplace_web, "get_image",
                               return_value=("image/jpeg", b"image")),
                  patch.object(marketplace_web, "read_manifest",
                               return_value={"id": "scan-other"}),
                  patch.object(marketplace_web, "public_scan",
                               return_value={"id": "scan-other"}),
                  patch.object(marketplace_web.Handler, "log_message")):
                server = ThreadingHTTPServer(("127.0.0.1", 0),
                                             marketplace_web.Handler)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    def request(path, token=None, spoof=None):
                        connection = HTTPConnection("127.0.0.1", server.server_port)
                        headers = {"Host": "scout.example.com"}
                        if token:
                            headers["Cf-Access-Jwt-Assertion"] = token
                        if spoof:
                            headers["X-Scout-Email"] = spoof
                        connection.request("GET", "/notify" + path,
                                           headers=headers)
                        response = connection.getresponse()
                        body = response.read()
                        result = response.status, (json.loads(body)
                                                   if response.getheader("Content-Type", "").startswith("application/json")
                                                   else body.decode("utf-8"))
                        connection.close()
                        return result

                    self.assertEqual(request("/api/admin")[0], 401)
                    legacy = HTTPConnection("127.0.0.1", server.server_port)
                    legacy.request("GET", "/Facebooknotify/api/scans",
                                   headers={"Host": "scout.example.com"})
                    self.assertEqual(legacy.getresponse().status, 401)
                    legacy.close()
                    self.assertEqual(request("/api/admin", "member@example.com",
                                             spoof="admin@example.com")[0], 403)
                    self.assertEqual(request("/admin", "member@example.com")[0], 403)
                    self.assertEqual(request("/comparables/couches", "member@example.com")[0], 403)
                    self.assertEqual(request("/api/comparables/couches", "member@example.com")[0], 403)
                    self.assertEqual(request("/api/comparables/couches/images/123", "member@example.com")[0], 403)
                    self.assertEqual(request("/comparables/general", "member@example.com")[0], 403)
                    self.assertEqual(request("/api/comparables/general", "member@example.com")[0], 403)
                    self.assertEqual(request("/api/comparables/general/images/456", "member@example.com")[0], 403)
                    status, overview = request("/api/admin", "admin@example.com")
                    self.assertEqual(status, 200)
                    self.assertEqual(overview["admin_emails"], ["admin@example.com"])
                    self.assertEqual(request("/admin", "admin@example.com")[0], 200)
                    self.assertEqual(request("/comparables/couches", "admin@example.com")[0], 200)
                    self.assertEqual(request("/api/comparables/couches", "admin@example.com")[1]["listings"][0]["listing_id"], "123")
                    self.assertEqual(request("/api/comparables/couches/images/123", "admin@example.com"), (200, "image"))
                    self.assertEqual(request("/api/comparables/couches/images/456", "admin@example.com")[0], 404)
                    self.assertEqual(request("/comparables/general", "admin@example.com")[0], 200)
                    self.assertEqual(request("/api/comparables/general", "admin@example.com")[1]["listings"][0]["listing_id"], "456")
                    self.assertEqual(request("/api/comparables/general/images/456", "admin@example.com"), (200, "image"))
                    self.assertEqual(request("/api/comparables/general/images/123", "admin@example.com")[0], 404)
                    self.assertEqual(request("/api/account", "admin@example.com")[1]["role"],
                                     "admin")
                    self.assertEqual(request("/api/account", "member@example.com")[1]["role"],
                                     "user")
                    self.assertEqual(request("/api/scans/scan-other", "member@example.com")[0],
                                     404)
                    self.assertEqual(request("/api/scans/scan-other", "admin@example.com")[0],
                                     200)
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(timeout=2)

    def test_verified_public_profiles_ignore_untrusted_email_and_scope_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            first = open_account("first@example.com", db)
            second = open_account("second@example.com", db)
            manifests = [
                {"id": "scan-a", "account_id": first["id"],
                 "zip_code": "21009", "radius_miles": 15},
                {"id": "scan-b", "account_id": second["id"],
                 "zip_code": "21009", "radius_miles": 15},
            ]
            def verified(token, _team, _aud):
                if not token:
                    raise marketplace_web.AccessDenied("Cloudflare Access login is required")
                return {"email": token, "subject": "verified-user"}

            with (patch.object(marketplace_web, "PUBLIC_HOST", "scout.example.com"),
                  patch.object(marketplace_web, "PUBLIC_WRITES", True),
                  patch.object(marketplace_web, "verify_access_token",
                               side_effect=verified),
                  patch.object(marketplace_web, "get_account",
                               side_effect=lambda email=None, **kw: get_account(email, db, **kw)),
                  patch.object(marketplace_web, "open_account",
                               side_effect=lambda email, **kw: open_account(email, db, **kw)),
                  patch.object(marketplace_web, "save_account",
                               side_effect=lambda payload, **kw: payload),
                  patch.object(marketplace_web, "scan_manifests",
                               return_value=manifests),
                  patch.object(marketplace_web, "scheduled_scan_owners",
                               return_value={}),
                  patch.object(marketplace_web.Handler, "log_message")):
                server = ThreadingHTTPServer(("127.0.0.1", 0),
                                             marketplace_web.Handler)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    def request(path, email=None, body=None, spoof=None):
                        connection = HTTPConnection("127.0.0.1", server.server_port)
                        headers = {"Host": "scout.example.com"}
                        if email:
                            headers["Cf-Access-Jwt-Assertion"] = email
                        if spoof:
                            headers["X-Scout-Email"] = spoof
                        if body is not None:
                            headers["Content-Type"] = "application/json"
                        connection.request("POST" if body is not None else "GET",
                                           "/notify" + path,
                                           body=json.dumps(body) if body is not None else None,
                                           headers=headers)
                        response = connection.getresponse()
                        result = response.status, json.loads(response.read())
                        connection.close()
                        return result

                    self.assertEqual(request("/api/scans")[0], 401)
                    self.assertEqual([row["id"] for row in
                                      request("/api/scans", "first@example.com",
                                              spoof="second@example.com")[1]["scans"]],
                                     ["scan-a"])
                    self.assertEqual([row["id"] for row in
                                      request("/api/scans", "second@example.com")[1]["scans"]],
                                     ["scan-b"])
                    self.assertEqual(request("/api/scans/scan-b",
                                             "first@example.com")[0], 404)
                    self.assertEqual(request("/api/account")[0], 401)
                    status, opened = request("/api/account/open",
                                             email="third@example.com", body={
                        "email": "second@example.com"})
                    self.assertEqual(status, 200)
                    self.assertTrue(opened["configured"])
                    self.assertEqual(open_account("third@example.com", db)["id"],
                                     opened["id"])
                    self.assertEqual(request("/api/scans", "third@example.com")[1]["scans"],
                                     [])
                    self.assertEqual(request("/api/account", "first@example.com",
                                             body={"email": "second@example.com"})[1]["email"],
                                     "first@example.com")
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(timeout=2)

    def test_alert_triage_is_scoped_to_verified_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            first = open_account("first@example.com", db)
            second = open_account("second@example.com", db)
            for account, scan_id in ((first, "scan-first"), (second, "scan-second")):
                self.assertEqual(record_scan_matches({
                    "id": scan_id, "account_id": account["id"], "status": "complete",
                    "zip_code": account["zip_code"],
                    "radius_miles": account["radius_miles"],
                    "listings": [{"id": scan_id, "title": "Wood chair",
                                  "item_type": "chair", "listing_kind": "product",
                                  "asking_price": 0, "distance_miles_approx": 2}],
                }, db), 1)
            first_alert = list_alerts(db, search_id=first["search_id"])["alerts"][0]
            second_alert = list_alerts(db, search_id=second["search_id"])["alerts"][0]

            with (patch.object(marketplace_web, "PUBLIC_HOST", "scout.example.com"),
                  patch.object(marketplace_web, "PUBLIC_WRITES", True),
                  patch.object(marketplace_web, "verify_access_token",
                               side_effect=lambda token, *_: {"email": token,
                                                                "subject": "verified"} if token else None),
                  patch.object(marketplace_web, "get_account",
                               side_effect=lambda email=None, **kw: get_account(email, db, **kw)),
                  patch.object(marketplace_web, "account_scans",
                               side_effect=lambda account: [{"id": "scan-first" if account["id"] == first["id"] else "scan-second"}]),
                  patch.object(marketplace_web, "list_alerts",
                               side_effect=lambda **kw: list_alerts(db, **kw)),
                  patch.object(marketplace_web, "set_alert_triage",
                               side_effect=lambda alert_id, status, **kw:
                                   set_alert_triage(alert_id, status, db, **kw)),
                  patch.object(marketplace_web, "hide_all_inbox_alerts",
                               side_effect=lambda search_id:
                                   marketplace_web_hide_all(search_id, db)),
                  patch.object(marketplace_web, "actionable_alerts",
                               side_effect=lambda payload: payload),
                  patch.object(marketplace_web.Handler, "log_message")):
                server = ThreadingHTTPServer(("127.0.0.1", 0), marketplace_web.Handler)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    def request(path, email, body=None):
                        connection = HTTPConnection("127.0.0.1", server.server_port)
                        headers = {"Host": "scout.example.com",
                                   "Cf-Access-Jwt-Assertion": email}
                        if body is not None:
                            headers["Content-Type"] = "application/json"
                        connection.request("POST" if body is not None else "GET",
                                           "/notify" + path,
                                           body=json.dumps(body) if body is not None else None,
                                           headers=headers)
                        response = connection.getresponse()
                        result = response.status, json.loads(response.read())
                        connection.close()
                        return result

                    self.assertEqual(request("/api/alerts", "first@example.com")[1]
                                     ["triage_counts"]["inbox"], 1)
                    self.assertEqual(request(f"/api/alerts/{second_alert['id']}/triage",
                                             "first@example.com", {"status": "hidden"})[0], 404)
                    self.assertEqual(request(f"/api/alerts/{first_alert['id']}/triage",
                                             "first@example.com", {"status": "saved"})[0], 200)
                    self.assertEqual(request("/api/alerts", "first@example.com")[1]["alerts"], [])
                    self.assertEqual(request("/api/alerts?view=saved", "first@example.com")
                                     [1]["alerts"][0]["id"], first_alert["id"])
                    self.assertEqual(request("/api/alerts?view=saved", "second@example.com")
                                     [1]["alerts"], [])
                    self.assertEqual(request("/api/alerts/hide-all", "second@example.com", {})
                                     [1]["hidden"], 1)
                    self.assertEqual(request("/api/alerts", "second@example.com")
                                     [1]["triage_counts"]["inbox"], 0)
                    self.assertEqual(request("/api/alerts?view=saved", "first@example.com")
                                     [1]["triage_counts"]["saved"], 1)
                    self.assertEqual(request("/api/alerts?view=invalid", "first@example.com")[0], 400)
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(timeout=2)

    def test_scan_settings_validate_zip_radius_and_keywords(self):
        self.assertEqual(validate_settings("21009", 15,
                                         ["free", "FREE", "desk"]),
                         ("21009", 15, ["free", "desk"]))
        self.assertEqual(len(validate_settings("21009", 15,
            [f"term {i}" for i in range(25)])[2]), 25)
        for args in (("2100", 15, ["free"]),
                     ("21009", 0, ["free"]),
                     ("21009", 101, ["free"]),
                     ("21009", 15, []),
                     ("21009", 15, [str(i) for i in range(26)]),
                     ("21009", 15, ["free\nattack"])):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_settings(*args)

    def test_result_file_must_stay_inside_runs(self):
        self.assertIsNone(result_path({"result_file": "../../Windows/win.ini"}))

    def test_cloudflare_path_keeps_routes_scoped_to_prefix(self):
        self.assertEqual(marketplace_web.route_path("/notify"),
                         ("/", "/notify"))
        self.assertEqual(marketplace_web.route_path("/notify/api/scans"),
                         ("/api/scans", "/notify"))
        self.assertEqual(marketplace_web.route_path("/Facebooknotify"),
                         ("/", "/Facebooknotify"))
        self.assertEqual(marketplace_web.route_path("/Facebooknotify/api/scans"),
                         ("/api/scans", "/Facebooknotify"))
        self.assertEqual(marketplace_web.route_path("/Facebooknotify-extra"),
                         ("/Facebooknotify-extra", ""))
        with (patch.object(marketplace_web, "PUBLIC_HOST", "scout.example.com"),
              patch.object(marketplace_web, "PUBLIC_WRITES", True),
              patch.object(marketplace_web.Handler, "_identity",
                           return_value={"email": "nobody@example.com",
                                         "subject": "verified-user"}),
              patch.object(marketplace_web.Handler, "log_message")):
            server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         marketplace_web.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def request(path, host, origin=None):
                    connection = HTTPConnection("127.0.0.1", server.server_port)
                    headers = {"Host": host}
                    if origin:
                        headers["Origin"] = origin
                    connection.request("GET" if origin is None else "POST",
                                       path, headers=headers)
                    response = connection.getresponse()
                    result = (response.status, response.read().decode("utf-8"))
                    connection.close()
                    return result

                status, html = request("/notify/", "scout.example.com")
                self.assertEqual(status, 200)
                self.assertIn('href="/notify/styles.css?v=', html)
                for asset in ("styles.css", "app.js"):
                    digest = hashlib.sha256(
                        (marketplace_web.WEB / asset).read_bytes()).hexdigest()[:12]
                    self.assertIn(f'/notify/{asset}?v={digest}', html)
                self.assertNotIn("__JS_VERSION__", html)
                self.assertIn('href="/notify/admin"', html)
                self.assertIn('data-public-writes="true"', html)
                self.assertEqual(request("/", "scout.example.com")[0], 403)
                self.assertEqual(request("/notify/api/admin",
                                         "scout.example.com")[0], 403)
                self.assertEqual(request("/notify/admin",
                                         "scout.example.com")[0], 403)
                self.assertEqual(request("/notify/api/scans",
                                         "scout.example.com")[0], 200)
                self.assertEqual(request("/notify/api/account",
                                         "scout.example.com",
                                         "https://another.example.com")[0], 403)
                self.assertEqual(request("/notify/app.js",
                                         "scout.example.com")[0], 200)
                self.assertEqual(request("/Facebooknotify/api/scans",
                                         "scout.example.com")[0], 308)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_public_route_defaults_to_view_only(self):
        with (patch.object(marketplace_web, "PUBLIC_HOST", "scout.example.com"),
              patch.object(marketplace_web, "PUBLIC_WRITES", False),
              patch.object(marketplace_web.Handler, "_identity",
                           return_value={"email": "nobody@example.com",
                                         "subject": "verified-user"}),
              patch.object(marketplace_web.Handler, "log_message")):
            server = ThreadingHTTPServer(("127.0.0.1", 0), marketplace_web.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def request(method, path):
                    connection = HTTPConnection("127.0.0.1", server.server_port)
                    connection.request(method, path, headers={"Host": "scout.example.com"})
                    response = connection.getresponse()
                    result = response.status, response.read().decode("utf-8")
                    connection.close()
                    return result

                status, html = request("GET", "/notify/")
                self.assertEqual(status, 200)
                self.assertIn('data-public-writes="false"', html)
                self.assertEqual(request("GET", "/notify/api/account")[0], 403)
                self.assertEqual(request("POST", "/notify/api/scans")[0], 403)
                self.assertEqual(request("DELETE", "/notify/api/searches/1")[0], 403)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_public_listing_keeps_ai_estimate_distinct_from_seller_text(self):
        listing = {
            "listing_id": "123", "title": "Desk",
            "description": "Has some marks", "refactored_description": "Has some marks.",
            "vision": {"observations": {"item_type": "desk"},
                       "review_flags": ["photo_review"]},
            "condition_assessment": {"result": {"usage_state": "used",
                                                "condition_grade": "fair"},
                                     "review_flags": ["photo_review", "weak_grade"]},
        }
        result = public_listing("scan-20261023-21009-15mi", listing)
        self.assertEqual(result["description"], "Has some marks")
        self.assertEqual(result["refactored_description"], "Has some marks.")
        self.assertEqual(result["condition_grade"], "fair")
        self.assertEqual(result["review_flags"], ["photo_review", "weak_grade"])
        self.assertEqual(result["image_url"],
                         "/api/scans/scan-20261023-21009-15mi/images/123")

    def test_results_show_only_physical_items(self):
        records = [
            {"listing_id": "1", "title": "Free couch", "asking_price": 0,
             "vision": {"observations": {"listing_kind": "product",
                                         "item_type": "couch"}}},
            {"listing_id": "2", "title": "Free pickup of scrap",
             "vision": {"observations": {"listing_kind": "service"}}},
            {"listing_id": "3", "title": "20% discount at store",
             "vision": {"observations": {"listing_kind": "mixed_or_unclear"},
                        "review_flags": ["store_promotion_not_single_item"]}},
            {"listing_id": "4", "title": "Unclassified",
             "vision": {"status": "error"}},
        ]
        manifest = {"id": "scan-20261023-21009-15mi"}
        with patch.object(marketplace_web, "result_listings",
                          return_value=({}, records)):
            result = public_scan(manifest)
        self.assertEqual([item["title"] for item in result["listings"]],
                         ["Free couch"])
        self.assertEqual(result["listings"][0]["display_title"], "Couch")
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["nearby_count"], 4)
        self.assertEqual(result["filtered_out_count"], 3)
        self.assertEqual(public_listing(manifest["id"], records[2])["listing_kind"],
                         "promotion")

    def test_running_scan_exposes_early_identification_without_final_grade(self):
        records = [
            {"listing_id": "1", "title": "Used table", "asking_price": 25,
             "vision": {"status": "observed", "observations": {
                 "listing_kind": "product", "item_type": "table"}}},
            {"listing_id": "2", "title": "Shed construction service",
             "vision": {"status": "filtered_offer", "observations": {
                 "listing_kind": "service"}}},
            {"listing_id": "3", "title": "Unprocessed card", "asking_price": 0},
        ]
        manifest = {"id": "scan-20261023-21009-15mi", "status": "running",
                    "stage": "Estimating new or used and condition"}
        with patch.object(marketplace_web, "result_listings",
                          return_value=({}, records)):
            preview = public_scan(manifest)
            complete = public_scan({**manifest, "status": "complete"})
        self.assertEqual([item["id"] for item in preview["listings"]], ["1"])
        self.assertEqual(preview["listings"][0]["analysis_state"], "preliminary")
        self.assertEqual(preview["listings"][0]["condition_grade"], "")
        self.assertEqual(preview["filtered_out_count"], 1)
        self.assertEqual(preview["pending_analysis_count"], 1)
        self.assertEqual(complete["listings"][0]["analysis_state"], "complete")

    def test_old_shed_ads_are_filtered_even_with_product_model_result(self):
        records = [
            {"listing_id": "1", "title": "Need shed built?",
             "description": "Shed building", "asking_price": 100,
             "vision": {"observations": {"listing_kind": "product",
                                         "item_type": "shed"}}},
            {"listing_id": "2", "title": "Shed Storage 8x12 Cottage $100 DOWN RENT TO OWN",
             "description": "RTO 36 months. No credit check", "asking_price": 100,
             "vision": {"observations": {"listing_kind": "product",
                                         "item_type": "shed"}}},
            {"listing_id": "3", "title": "Used shed for sale", "asking_price": 400,
             "vision": {"observations": {"listing_kind": "product",
                                         "item_type": "shed"}}},
            {"listing_id": "4", "title": "Yard sale", "asking_price": 0,
             "vision": {"observations": {"listing_kind": "product",
                                         "item_type": "shed"}}},
        ]
        with patch.object(marketplace_web, "result_listings",
                          return_value=({}, records)):
            result = public_scan({"id": "scan-20261023-21009-15mi"})
        self.assertEqual([item["id"] for item in result["listings"]], ["3"])
        self.assertEqual({row["id"]: row["reason"] for row in result["filtered_cards"]},
                         {"1": "Service advertisement",
                          "2": "Financing offer; listed amount is a down payment",
                          "4": "Event notice"})

    def test_old_saved_title_is_cleaned_when_scan_is_displayed(self):
        raw = "✨ FREE BEAUTIFUL VANITY WITH MIRROR! ✨"
        result = public_listing("scan-20261027-173020-21009-45mi", {
            "listing_id": "1994333354580024", "title": raw,
            "display_title": raw, "asking_price": 0,
            "vision": {"observations": {"item_type": "console_table",
                                        "listing_kind": "product"}},
        })
        self.assertEqual(result["title"], raw)
        self.assertEqual(result["display_title"], "Vanity with mirror")

    def test_old_mixed_lot_uses_offered_items_and_varied_condition(self):
        item = public_listing("scan-old", {
            "listing_id": "lot", "title": "Free stuff 3100 block Grindon Ave",
            "description": "Fan needs repair, shoe rack, scooter, dog bed.",
            "asking_price": 0,
            "vision": {"observations": {"listing_kind": "product",
                                        "item_type": "box"}},
            "condition_assessment": {"result": {"condition_grade": "needs_repair"}},
            "valuation": {"status": "provisional", "low": 10, "high": 20},
        })
        self.assertEqual(item["display_title"], "Fan, shoe rack, scooter and more")
        self.assertEqual(item["item_type"], "assorted_items")
        self.assertEqual(item["condition_grade"], "unknown")
        self.assertEqual(item["valuation"]["reason"], "mixed_item_lot")
        self.assertIn("mixed_item_lot", item["review_flags"])

    def test_old_nordictrack_listing_corrects_rower_type(self):
        item = public_listing("scan-old", {
            "listing_id": "ski", "title": "FREE Vintage NordicTrack Achiever Ski Machine � Parts/Repair",
            "description": "NordicTrack Achiever cross-country ski exercise machine.",
            "asking_price": 0,
            "vision": {"observations": {"listing_kind": "product",
                                        "item_type": "rowing_machine",
                                        "category": "sports_equipment",
                                        "visible_brand": "NordicTrack",
                                        "visible_model": "ACHIEVER"}},
        })
        self.assertEqual(item["display_title"],
                         "NordicTrack Achiever ski machine (parts/repair)")
        self.assertEqual(item["item_type"], "ski_machine")
        self.assertIn("seller_item_overrode_model_type", item["review_flags"])

    def test_old_oil_listing_headline_is_short_on_scan_page(self):
        item = public_listing("scan-old", {
            "listing_id": "oil", "asking_price": 0,
            "title": "Winter is coming! 32 gal. Of waste oil from automotive "
                     "oil changes Free Free Free all in gal. Jugs",
            "description": "Free come get it .",
            "vision": {"observations": {"listing_kind": "product",
                                        "item_type": "waste oil"}},
        })
        self.assertEqual(item["display_title"], "Used automotive oil in jugs")

    def test_known_item_with_no_price_group_has_separate_reasons(self):
        item = public_listing("scan-cleats", {
            "listing_id": "123", "title": "Nike baseball cleats",
            "description": "Wore one season. Need a good cleaning.",
            "image_files": ["one.jpg", "two.jpg", "three.jpg", "four.jpg"],
            "vision": {"observations": {"listing_kind": "product",
                                        "item_type": "baseball_cleats",
                                        "category_confidence": "high",
                                        "candidate_identity": "Nike Huarache",
                                        "identity_confidence": "high"}},
            "condition_assessment": {"result": {
                "usage_state": "used", "condition_grade": "unknown",
                "confidence": "low", "uncertainties": [
                    "One small listing image cannot confirm hidden wear or function."]}},
            "valuation": {"status": "unvalued",
                          "reason": "item_identity_unconfirmed"},
        })
        self.assertEqual(item["item_confidence"], "high")
        self.assertEqual(item["condition_confidence"], "low")
        self.assertEqual(item["price_confidence"], "unrated")
        self.assertEqual(item["valuation"]["reason"], "price_group_unavailable")
        self.assertIn("seller mentions wear or cleaning",
                      item["condition_reason"].lower())
        self.assertIn("4 listing photos", item["condition_uncertainties"][0])

    def test_saved_scan_rechecks_condition_when_seller_describes_defect(self):
        item = public_listing("scan-old", {
            "listing_id": "recliner", "title": "Leather recliner",
            "description": "Cushion is in great shape but it currently does not recline.",
            "vision": {"observations": {"listing_kind": "product",
                                        "item_type": "recliner"}},
            "condition_assessment": {"status": "estimated", "result": {
                "usage_state": "used", "condition_grade": "good",
                "functional_status": "unknown", "confidence": "medium"}},
        })
        self.assertEqual(item["condition_grade"], "needs_repair")
        self.assertIn("repair_grade_from_seller_text", item["review_flags"])

    def test_saved_scan_keeps_pet_without_grading_and_filters_unidentified_lot(self):
        records = [
            {"listing_id": "pet", "title": "Dog to Good Home",
             "description": "Giving a dog away to a good home.",
             "vision": {"observations": {"listing_kind": "product",
                                         "item_type": "dog"}}},
            {"listing_id": "stuff", "title": "Free @ the curb 170 Springvale Road",
             "description": "Free help yourself. More stuff being added.",
             "vision": {"observations": {"listing_kind": "product",
                                         "item_type": "box"}}},
        ]
        with patch.object(marketplace_web, "result_listings",
                          return_value=({}, records)):
            result = public_scan({"id": "scan-old"})
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["listings"][0]["id"], "pet")
        self.assertTrue(result["listings"][0]["pet_listing"])
        self.assertEqual(result["listings"][0]["condition_grade"], "")
        self.assertEqual(result["listings"][0]["valuation"]["reason"], "pet_listing")
        self.assertIn("No identifiable item", result["filtered_cards"][0]["reason"])

    def test_zero_price_card_with_cash_payment_is_not_labeled_free(self):
        item = public_listing("scan-old", {
            "listing_id": "organ", "title": "Vintage organ",
            "description": "Cash and pick up only.", "asking_price": 0,
            "vision": {"observations": {"listing_kind": "product",
                                        "item_type": "organ"}},
        })
        self.assertIsNone(item["asking_price"])
        self.assertEqual(item["price_source"], "unconfirmed")
        self.assertIn("zero_price_card_may_not_be_free", item["review_flags"])

    def test_explicit_free_offer_clears_stale_price_conflict(self):
        for title, description in (
                ("Free Piano", "Free Piano. You haul it. It's free."),
                ("Free Chefman Microwave", "Retails for $80. No dish included.")):
            with self.subTest(title=title):
                item = public_listing("scan-old", {
                    "listing_id": "free", "title": title,
                    "description": description, "asking_price": 0,
                    "vision": {"observations": {"listing_kind": "product"},
                               "review_flags": ["zero_price_card_may_not_be_free"]},
                })
                self.assertEqual(item["asking_price"], 0)
                self.assertEqual(item["price_source"], "facebook_card")
                self.assertNotIn("zero_price_card_may_not_be_free",
                                 item["review_flags"])

    def test_title_asking_price_resolves_zero_price_card(self):
        item = public_listing("scan-old", {
            "listing_id": "chair", "title": "Chair asking $25",
            "description": "Pickup in Edgewood", "asking_price": 0,
            "vision": {"observations": {"listing_kind": "product"}},
        })
        self.assertEqual(item["asking_price"], 25)
        self.assertEqual(item["price_source"], "seller_title")

    def test_conflicting_title_and_description_asks_remain_unconfirmed(self):
        item = public_listing("scan-old", {
            "listing_id": "chair", "title": "Chair asking $25",
            "description": "Asking $40. Pickup in Edgewood", "asking_price": 0,
            "vision": {"observations": {"listing_kind": "product"}},
        })
        self.assertIsNone(item["asking_price"])
        self.assertEqual(item["price_source"], "unconfirmed")

    def test_price_only_title_is_not_accepted_as_seller_ask(self):
        item = public_listing("scan-old", {
            "listing_id": "speaker", "title": "$5",
            "description": "Old computer speakers. They work.", "asking_price": 0,
            "vision": {"observations": {"listing_kind": "product"}},
        })
        self.assertIsNone(item["asking_price"])
        self.assertEqual(item["price_source"], "unconfirmed")

    def test_running_scan_reports_checkpointed_model_progress(self):
        manifest = {"id": "scan-progress", "status": "running",
                    "stage": "Identifying items from photos and text",
                    "within_radius_count": 3}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / manifest["id"]
            folder.mkdir()
            (folder / "vision.json").write_text(json.dumps({"listings": [
                {"vision": {"status": "observed"}},
                {"vision": {"status": "error"}},
                {},
            ]}), encoding="utf-8")
            with patch.object(marketplace_web, "SCAN_ROOT", Path(directory)):
                self.assertEqual(scan_stage_progress(manifest), {
                    "label": "Photo identification", "processed": 2, "total": 3})
                manifest["stage"] = "Estimating new or used and condition"
                (folder / "conditioned.json").write_text(json.dumps({"listings": [
                    {"condition_assessment": {"status": "estimated"}}, {}, {},
                ]}), encoding="utf-8")
                self.assertEqual(scan_stage_progress(manifest)["processed"], 1)

    def test_watch_scan_result_respects_its_price_range(self):
        records = [
            {"listing_id": "1", "title": "Desk", "asking_price": 40,
             "vision": {"observations": {"listing_kind": "product"}}},
            {"listing_id": "2", "title": "Desk", "asking_price": 140,
             "vision": {"observations": {"listing_kind": "product"}}},
        ]
        manifest = {"id": "scan-20261023-21009-15mi",
                    "watch_item": {"term": "desk", "min_price": 20,
                                   "max_price": 100}}
        with patch.object(marketplace_web, "result_listings",
                          return_value=({}, records)):
            result = public_scan(manifest)
        self.assertEqual([item["id"] for item in result["listings"]], ["1"])

    def test_zero_price_scooter_uses_explicit_seller_ask(self):
        scooter = {
            "listing_id": "2201823493934335", "title": "electric scooter",
            "asking_price": 0, "previous_price": 200,
            "description": "350w motor comes with charger. it's not free. asking $180",
            "vision": {"observations": {"listing_kind": "product",
                                        "item_type": "electric scooter"},
                       "review_flags": ["zero_price_card_may_not_be_free"]},
        }
        item = public_listing("scan-price-conflict", scooter)
        self.assertEqual(item["asking_price"], 180)
        self.assertEqual(item["card_asking_price"], 0)
        self.assertEqual(item["seller_asking_price"], 180)
        self.assertEqual(item["price_source"], "seller_description")
        manifest = {"id": "scan-price-conflict", "watch_item": {
            "term": "electric scooter", "min_price": 0, "max_price": 30}}
        with patch.object(marketplace_web, "result_listings",
                          return_value=({}, [scooter])):
            result = public_scan(manifest)
        self.assertEqual(result["result_count"], 0)
        self.assertIn("Seller asks $180", result["filtered_cards"][0]["reason"])

    def test_zero_price_card_with_unstated_ask_is_not_free(self):
        item = public_listing("scan-price-conflict", {
            "listing_id": "1", "title": "Chair", "asking_price": 0,
            "description": "Not free; message me for a price.",
            "vision": {"observations": {"listing_kind": "product"}},
        })
        self.assertIsNone(item["asking_price"])
        self.assertEqual(item["price_source"], "unconfirmed")
        self.assertIn("zero_price_card_may_not_be_free", item["review_flags"])

    def test_price_skipped_card_is_explained_in_filtered_list(self):
        manifest = {"id": "scan-price-filter", "watch_item": {
            "term": "scooter", "min_price": 0, "max_price": 30}}
        records = [{"listing_id": "1", "title": "Electric scooter",
                    "description": "Runs well", "asking_price": 60,
                    "vision": {"status": "skipped_price_range"}}]
        with patch.object(marketplace_web, "result_listings",
                          return_value=({}, records)):
            result = public_scan(manifest)
        self.assertEqual(result["result_count"], 0)
        self.assertEqual(result["nearby_count"], 1)
        self.assertIn("$60", result["filtered_cards"][0]["reason"])

    def test_scan_quick_lists_explain_all_scanned_and_filtered_cards(self):
        manifest = {"id": "scan-20261023-21009-15mi",
                    "watch_item": {"term": "scooter", "min_price": 0,
                                   "max_price": 30}}
        nearby = [
            {"listing_id": "1", "title": "Scooter", "asking_price": 50,
             "url": "https://www.facebook.com/marketplace/item/1/",
             "description": "Runs well", "vision": {
                 "observations": {"listing_kind": "product"}}},
            {"listing_id": "2", "title": "Scooter service",
             "description": "Repairs scooters", "vision": {
                 "observations": {"listing_kind": "service"}}},
            {"listing_id": "3", "title": "Scooter parts",
             "description": "Box of parts", "vision": {"status": "error"}},
        ]
        collected = {"listings": [
            {"listing_id": "1", "title": "Scooter"},
            {"listing_id": "2", "title": "Scooter service"},
            {"listing_id": "3", "title": "Scooter parts"},
            {"listing_id": "4", "title": "Distant scooter"},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / manifest["id"]
            folder.mkdir()
            (folder / "collection.json").write_text(json.dumps(collected),
                                                     encoding="utf-8")
            with (patch.object(marketplace_web, "SCAN_ROOT", Path(directory)),
                  patch.object(marketplace_web, "result_listings",
                               return_value=({}, nearby))):
                result = public_scan(manifest)
        self.assertEqual(len(result["scanned_cards"]), 4)
        self.assertEqual(result["scanned_cards"][0]["description"], "Runs well")
        self.assertEqual(result["scanned_cards"][0]["url"],
                         "https://www.facebook.com/marketplace/item/1/")
        self.assertEqual(result["scanned_cards"][3]["title"], "Distant scooter")
        self.assertIn("outside the radius", result["scanned_cards"][3]["description"])
        self.assertEqual(len(result["filtered_cards"]), 3)
        self.assertIn("$50", result["filtered_cards"][0]["reason"])
        self.assertEqual(result["filtered_cards"][0]["url"],
                         "https://www.facebook.com/marketplace/item/1/")
        self.assertEqual(result["filtered_cards"][1]["reason"],
                         "Service advertisement")
        self.assertEqual(result["filtered_cards"][2]["reason"],
                         "AI photo analysis incomplete")
        self.assertEqual(result["ai_error_count"], 1)

    def test_scan_shows_nearest_outside_cards_without_treating_them_as_matches(self):
        manifest = {"id": "scan-outside-21050", "zip_code": "21050",
                    "radius_miles": 15, "status": "complete"}
        data = {"excluded_listings": [
            {"listing_id": "2", "title": "Log splitter", "location": "Hanover, PA",
             "asking_price": 200, "radius_check": {
                 "distance_miles_approx": 35.7, "near_boundary": False}},
            {"listing_id": "1", "title": "Wood splitting service - let me help",
             "location": "Havre de Grace, MD", "asking_price": 60,
             "radius_check": {"distance_miles_approx": 15.43,
                              "near_boundary": True}},
        ], "unresolved_listings": [{"listing_id": "3"}]}
        with patch.object(marketplace_web, "result_listings",
                          return_value=(data, [])):
            result = public_scan(manifest)
        self.assertEqual(result["result_count"], 0)
        self.assertEqual(result["nearby_count"], 0)
        self.assertEqual([row["id"] for row in result["closest_outside"]], ["2"])
        self.assertEqual(result["unresolved_location_count"], 1)

    def test_historical_rental_listing_is_not_shown_as_item(self):
        item = public_listing("scan-rental", {
            "listing_id": "123", "title": "Storage Shed",
            "description": "New storage shed available to rent for $95 a month.",
            "asking_price": 95,
            "vision": {"observations": {"listing_kind": "product",
                                        "item_type": "shed"}},
        })
        self.assertEqual(item["listing_kind"], "service")
        self.assertIn("rental_offer_not_item_sale", item["review_flags"])

    def test_scan_archive_counts_items_shown_after_all_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            for scan_id, status in (("scan-complete", "complete"),
                                    ("scan-running", "running")):
                folder = Path(directory) / scan_id
                folder.mkdir()
                (folder / "manifest.json").write_text(json.dumps({
                    "id": scan_id, "status": status, "within_radius_count": 3,
                    "created_at": "2026-10-23T00:00:00Z"}), encoding="utf-8")
            with (patch.object(marketplace_web, "SCAN_ROOT", Path(directory)),
                  patch.object(marketplace_web, "public_scan",
                               side_effect=lambda manifest: (
                                   {"result_count": 1, "listings": [
                                       {"id": "123", "display_title": "Chair"}]}
                                   if manifest["status"] == "running" else
                                   {"result_count": 0, "listings": []})) as detail):
                scans = marketplace_web.scan_manifests()
        by_id = {scan["id"]: scan for scan in scans}
        self.assertEqual(by_id["scan-complete"]["result_count"], 0)
        self.assertEqual(by_id["scan-complete"]["within_radius_count"], 3)
        self.assertEqual(by_id["scan-running"]["result_count"], 1)
        self.assertEqual(by_id["scan-running"]["early_items"],
                         [{"id": "123", "title": "Chair"}])
        self.assertEqual(detail.call_count, 2)

    def test_scan_watchlist_now_starts_first_and_queues_remaining_items(self):
        items = [{"term": "couch", "zip_code": "21009", "radius_miles": 15,
                  "min_price": 0, "max_price": 100},
                 {"term": "desk", "zip_code": "10001", "radius_miles": 5,
                  "min_price": 20, "max_price": 80}]
        with (patch.object(marketplace_web, "active_scan_id", None),
              patch.object(marketplace_web, "start_scan",
                           return_value={"id": "first"}) as start,
              patch.object(marketplace_web, "claim_slot") as claim):
            result = marketplace_web.queue_account_scans(
                {"id": "demo", "watch_items": items})
        self.assertEqual(result["queued_count"], 2)
        self.assertEqual(result["id"], "first")
        start.assert_called_once_with({"zip_code": "21009", "radius_miles": 15,
            "keywords": ["couch"], "watch_item": items[0], "mode": "scheduled"},
            account_id="demo", email_batch_key=ANY, email_batch_size=2)
        self.assertEqual(claim.call_args.args[0].split(":item:")[0],
                         start.call_args.kwargs["email_batch_key"])
        self.assertEqual(claim.call_count, 1)
        self.assertEqual(claim.call_args.kwargs["watch_item"], items[1])

    def test_start_scan_saves_settings_and_dispatches_local_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            started = threading.Event()
            fake = {"id": "scan-20261023-21009-15mi", "zip_code": "21009",
                    "radius_miles": 15, "keywords": ["free"]}
            with (patch.object(marketplace_web, "ROOT", Path(directory)),
                  patch.object(marketplace_web, "create_manifest", return_value=fake),
                  patch.object(marketplace_web, "run_scan",
                               side_effect=lambda _scan_id: (
                                   started.set() or {"status": "failed"})),
                  patch.object(marketplace_web, "active_scan_id", None)):
                result = marketplace_web.start_scan({"zip_code": "21009",
                                                     "radius_miles": 15,
                                                     "keywords": ["free"]})
                self.assertTrue(started.wait(2))
                settings = json.loads((Path(directory) / "marketplace_settings.json")
                                      .read_text(encoding="utf-8"))
                self.assertEqual(settings, {"zip_code": "21009",
                                            "radius_miles": 15,
                                            "keywords": ["free"]})
                self.assertEqual(result["id"], fake["id"])

    def test_unexpected_worker_error_fails_scan_and_clears_active_state(self):
        scan = {"id": "scan-worker-crash", "status": "running",
                "stage": "Reading seller descriptions"}
        finished = threading.Event()
        saved = []

        def notify(completed):
            saved.append(dict(completed))
            finished.set()

        with (patch.object(marketplace_web, "active_scan_id", scan["id"]),
              patch.object(marketplace_web, "run_scan",
                           side_effect=OSError("disk unavailable")),
              patch.object(marketplace_web, "read_manifest", return_value=scan),
              patch.object(marketplace_web, "save_manifest") as persist,
              patch.object(marketplace_web, "complete_scan_notifications",
                           side_effect=notify)):
            marketplace_web.launch_scan_worker(scan)
            self.assertTrue(finished.wait(2))
            for _ in range(100):
                if marketplace_web.active_scan_id is None:
                    break
                time.sleep(0.01)
            self.assertIsNone(marketplace_web.active_scan_id)
        self.assertEqual(saved[0]["status"], "failed")
        self.assertIn("disk unavailable", saved[0]["error"])
        persist.assert_called_once()

    def test_worker_error_does_not_overwrite_completed_result(self):
        scan = {"id": "scan-already-complete", "status": "complete"}
        finished = threading.Event()
        with (patch.object(marketplace_web, "active_scan_id", scan["id"]),
              patch.object(marketplace_web, "run_scan",
                           side_effect=RuntimeError("late error")),
              patch.object(marketplace_web, "read_manifest", return_value=scan),
              patch.object(marketplace_web, "save_manifest") as persist,
              patch.object(marketplace_web, "complete_scan_notifications",
                           side_effect=lambda _scan: finished.set())):
            marketplace_web.launch_scan_worker(scan)
            self.assertTrue(finished.wait(2))
            for _ in range(100):
                if marketplace_web.active_scan_id is None:
                    break
                time.sleep(0.01)
            self.assertIsNone(marketplace_web.active_scan_id)
        self.assertEqual(scan["status"], "complete")
        persist.assert_not_called()

    def test_worker_launch_failure_marks_scan_failed(self):
        scan = {"id": "scan-cannot-launch", "status": "queued"}
        with (patch.object(marketplace_web, "active_scan_id", scan["id"]),
              patch.object(marketplace_web.threading, "Thread") as thread,
              patch.object(marketplace_web, "read_manifest", return_value=scan),
              patch.object(marketplace_web, "save_manifest") as persist,
              patch.object(marketplace_web, "complete_scan_notifications") as notify):
            thread.return_value.start.side_effect = OSError("no threads")
            with self.assertRaisesRegex(RuntimeError, "Could not start"):
                marketplace_web.launch_scan_worker(scan)
            self.assertIsNone(marketplace_web.active_scan_id)
        self.assertEqual(scan["status"], "failed")
        persist.assert_called_once()
        notify.assert_called_once_with(scan)

    def test_notification_recovery_retries_transient_alert_failure_once(self):
        scan = {"id": "scan-recover-notify", "status": "complete",
                "keywords": ["mower"]}
        with tempfile.TemporaryDirectory() as directory:
            scan_root = Path(directory)
            folder = scan_root / scan["id"]
            folder.mkdir()
            manifest_file = folder / "manifest.json"
            manifest_file.write_text(json.dumps(scan), encoding="utf-8")
            def persist(completed):
                manifest_file.write_text(json.dumps(completed), encoding="utf-8")
            with (patch.object(marketplace_web, "SCAN_ROOT", scan_root),
              patch.object(marketplace_web, "public_scan",
                           return_value={**scan, "listings": []}),
              patch.object(marketplace_web, "read_manifest", return_value=scan),
              patch.object(marketplace_web, "record_scan_matches",
                           side_effect=[OSError("database busy"), 0]) as record,
              patch.object(marketplace_web, "finish_email_scan",
                           return_value=False) as finish,
              patch.object(marketplace_web, "save_manifest",
                           side_effect=persist) as saved):
                marketplace_web.recover_email_notifications()
                self.assertNotIn("notifications_reconciled_at", scan)
                marketplace_web.recover_email_notifications()
                self.assertIn("notifications_reconciled_at", scan)
                marketplace_web.recover_email_notifications()
        self.assertEqual(record.call_count, 2)
        finish.assert_called_once()
        saved.assert_called_once()

    def test_completed_scan_dispatches_alert_matching(self):
        with tempfile.TemporaryDirectory() as directory:
            matched = threading.Event()
            fake = {"id": "scan-20261023-21009-15mi", "zip_code": "21009",
                    "radius_miles": 15, "keywords": ["free"],
                    "status": "complete"}
            with (patch.object(marketplace_web, "ROOT", Path(directory)),
                  patch.object(marketplace_web, "create_manifest", return_value=fake),
                  patch.object(marketplace_web, "run_scan", return_value=fake),
                  patch.object(marketplace_web, "public_scan",
                               return_value={**fake, "listings": []}),
                  patch.object(marketplace_web, "record_scan_matches",
                               side_effect=lambda _scan: matched.set()),
                  patch.object(marketplace_web, "active_scan_id", None)):
                marketplace_web.start_scan({"zip_code": "21009",
                                            "radius_miles": 15,
                                            "keywords": ["free"]})
                self.assertTrue(matched.wait(2))

    def test_owned_completed_scan_queues_match_email(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "alerts.sqlite"
            account = save_account({"email": "buyer@example.com",
                                    "zip_code": "21009", "radius_miles": 15,
                                    "terms": ["chair"], "plan": "free",
                                    "timezone": "America/New_York"}, db)
            manifest = {"id": "scan-email", "account_id": account["id"],
                        "zip_code": "21009", "radius_miles": 15,
                        "keywords": ["chair"], "status": "complete"}
            listing = {"id": "chair-1", "title": "Wood chair",
                       "item_type": "chair", "listing_kind": "product",
                       "condition_grade": "good", "distance_miles_approx": 2,
                       "asking_price": 0}
            finished = threading.Event()

            def finish(scan):
                try:
                    return finish_email_scan(scan, db)
                finally:
                    finished.set()

            with (patch.dict("os.environ", {"SCOUT_PUBLIC_HOST": "scout.example.com"}),
                  patch.object(marketplace_web, "ROOT", root),
                  patch.object(marketplace_web, "create_manifest", return_value=manifest),
                  patch.object(marketplace_web, "save_manifest"),
                  patch.object(marketplace_web, "run_scan", return_value=manifest),
                  patch.object(marketplace_web, "public_scan",
                               side_effect=lambda completed:
                               {**completed, "listings": [listing]}),
                  patch.object(marketplace_web, "prepare_email_batch",
                               side_effect=lambda account_id, key, size:
                               prepare_email_batch(account_id, key, size, db)),
                  patch.object(marketplace_web, "record_scan_matches",
                               side_effect=lambda scan: record_scan_matches(scan, db)),
                  patch.object(marketplace_web, "finish_email_scan",
                               side_effect=finish),
                  patch.object(marketplace_web, "deliver_pending_summaries",
                               return_value=0),
                  patch.object(marketplace_web, "active_scan_id", None)):
                marketplace_web.start_scan({"zip_code": "21009", "radius_miles": 15,
                                            "keywords": ["chair"]},
                                           account_id=account["id"])
                self.assertTrue(finished.wait(2))
            self.assertEqual(email_status(db)["pending"], 1)

    def test_due_slot_starts_one_bounded_scheduled_scan(self):
        couch = {"term": "couch", "zip_code": "21009", "radius_miles": 15,
                 "min_price": 0, "max_price": 100}
        desk = {"term": "desk", "zip_code": "10001", "radius_miles": 5,
                "min_price": 20, "max_price": 80}
        account = {"configured": True, "zip_code": "21009",
                   "radius_miles": 15, "terms": ["couch", "desk"],
                   "watch_items": [couch, desk],
                   "plan": "free", "timezone": "America/New_York"}
        account["id"] = "demo"
        with (patch.object(marketplace_web, "list_account_profiles",
                           return_value=[account]),
              patch.object(marketplace_web, "load_pilot", return_value=None),
              patch.object(marketplace_web, "due_slot",
                           return_value="2026-10-23-07"),
              patch.object(marketplace_web, "claim_slot", return_value=True) as claim,
              patch.object(marketplace_web, "pending_slots",
                           return_value=[{"account_id": "demo",
                                          "slot_key": "2026-10-23-07:item:one",
                                          "watch_item": couch}]),
              patch.object(marketplace_web, "start_scan",
                           return_value={"id": "scan-scheduled"}) as start,
              patch.object(marketplace_web, "finish_slot") as finish,
              patch.object(marketplace_web, "active_scan_id", None)):
            self.assertEqual(marketplace_web.run_due_schedule(), "scan-scheduled")
            self.assertEqual(claim.call_count, 2)
            self.assertEqual([call.kwargs["watch_item"] for call in claim.call_args_list],
                             [couch, desk])
            start.assert_called_once_with({
                "zip_code": "21009", "radius_miles": 15,
                "keywords": ["couch"], "watch_item": couch,
                "mode": "scheduled"},
                account_id="demo", email_batch_key="2026-10-23-07",
                email_batch_size=2,
                scheduled_slot_key="2026-10-23-07:item:one",
                retry_root=None, retry_attempt=0)
            finish.assert_called_once_with("2026-10-23-07:item:one", "scan-scheduled",
                                           account_id="demo")

    def test_admin_overview_distinguishes_owned_area_and_other_scans(self):
        profiles = [{"id": "demo", "display_name": "Demo account",
                     "plan": "plus_preview", "zip_code": "21009",
                     "radius_miles": 15}]
        manifests = [
            {"id": "owned", "account_id": "demo", "zip_code": "21009",
             "radius_miles": 15},
            {"id": "older", "zip_code": "21009", "radius_miles": 15},
            {"id": "elsewhere", "zip_code": "10001", "radius_miles": 10},
        ]
        result = marketplace_web.admin_overview(profiles, manifests, {})
        self.assertEqual(result["paid_subscriber_count"], 0)
        self.assertEqual(result["preview_count"], 1)
        self.assertEqual(result["accounts"][0]["daily_scan_times"],
                         ["07:00", "12:00"])
        self.assertEqual([(row["id"], row["relationship"]) for row in
                          result["accounts"][0]["scans"]],
                         [("owned", "account"), ("older", "area_match")])
        self.assertEqual([row["id"] for row in result["unassigned_scans"]],
                         ["elsewhere"])

    def test_new_email_profile_does_not_inherit_legacy_area_scans(self):
        manifests = [
            {"id": "legacy", "zip_code": "21009", "radius_miles": 15},
            {"id": "mine", "account_id": "acct-new", "zip_code": "21009",
             "radius_miles": 15},
        ]
        with patch.object(marketplace_web, "scheduled_scan_owners",
                          return_value={}):
            scans = marketplace_web.account_scans(
                {"configured": True, "id": "acct-new", "zip_code": "21009",
                 "radius_miles": 15}, manifests)
        self.assertEqual([scan["id"] for scan in scans], ["mine"])

    def test_demo_email_profile_only_lists_scans_with_recorded_ownership(self):
        manifests = [
            {"id": "owned", "account_id": "demo", "zip_code": "21009",
             "radius_miles": 15},
            {"id": "same-area", "zip_code": "21009", "radius_miles": 15},
        ]
        with patch.object(marketplace_web, "scheduled_scan_owners",
                          return_value={}):
            scans = marketplace_web.account_scans(
                {"configured": True, "id": "demo", "zip_code": "21009",
                 "radius_miles": 15}, manifests)
        self.assertEqual([scan["id"] for scan in scans], ["owned"])

    def test_account_queue_shows_running_and_each_waiting_keyword(self):
        items = [
            {"term": "scooter", "zip_code": "21009", "radius_miles": 15,
             "min_price": 0, "max_price": 30},
            {"term": "mower", "zip_code": "21009", "radius_miles": 15,
             "min_price": 0, "max_price": 50},
        ]
        scans = [{"id": "scan-free", "status": "running", "keywords": ["free"],
                  "zip_code": "21009", "radius_miles": 15}]
        slots = [{"account_id": "demo", "claimed_at": "2026-10-23T20:00:00Z",
                  "watch_item": items[0]},
                 {"account_id": "other", "claimed_at": "2026-10-23T20:00:01Z",
                  "watch_item": items[0]},
                 {"account_id": "demo", "claimed_at": "2026-10-23T20:00:02Z",
                  "watch_item": items[1]}]
        slots.append({"account_id": "demo", "claimed_at": "2026-10-23T20:00:03Z",
                      "watch_item": {**items[0], "term": "old scooter"}})
        with patch.object(marketplace_web, "pending_slots", return_value=slots):
            queue = marketplace_web.account_scan_queue(
                {"configured": True, "id": "demo", "watch_items": items}, scans)
        self.assertEqual([row["keywords"] for row in queue],
                         [["free"], ["scooter"], ["mower"]])
        self.assertEqual([row["status"] for row in queue],
                         ["running", "queued", "queued"])
        self.assertEqual([row["id"] for row in queue], ["scan-free", None, None])

    def test_fast_priority_slot_starts_incremental_scan(self):
        item = {"term": "mower", "zip_code": "21009", "radius_miles": 15,
                "min_price": 0, "max_price": 100, "fast": True}
        account = {"id": "buyer", "configured": True, "plan": "plus_preview",
                   "timezone": "America/New_York", "watch_items": [item]}
        now = datetime(2026, 10, 23, 14, 30, tzinfo=timezone.utc)
        with (patch.object(marketplace_web, "list_account_profiles", return_value=[account]),
              patch.object(marketplace_web, "pending_slots", return_value=[]),
              patch.object(marketplace_web, "claim_slot") as claim,
              patch.object(marketplace_web, "start_next_pending", return_value=None)):
            marketplace_web.run_due_schedule(now)
        self.assertTrue(claim.call_args.args[0].startswith(
            "fast-2026-10-23-10-30:item:"))
        self.assertEqual(claim.call_args.kwargs["watch_item"], item)

        pending = [{"account_id": "buyer", "slot_key": claim.call_args.args[0],
                    "watch_item": item}]
        with (patch.object(marketplace_web, "pending_slots", return_value=pending),
              patch.object(marketplace_web, "active_scan_id", None),
              patch.object(marketplace_web, "start_scan",
                           return_value={"id": "scan-fast"}) as started,
              patch.object(marketplace_web, "finish_slot") as finished):
            self.assertEqual(marketplace_web.start_next_pending({"buyer": account}),
                             "scan-fast")
        self.assertEqual(started.call_args.args[0]["mode"], "incremental")
        self.assertEqual(started.call_args.kwargs["email_batch_size"], 1)
        finished.assert_called_once()


if __name__ == "__main__":
    unittest.main()
