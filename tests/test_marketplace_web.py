"""The local scan site validates requests and keeps untrusted data bounded."""

import unittest
import json
from pathlib import Path
import tempfile
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import ANY, patch

from marketplace_scan_runner import validate_settings
from marketplace_alerts import (
    get_account, grant_admin, is_admin_email, list_admin_emails, open_account,
    record_scan_matches, save_account,
)
from marketplace_email import email_status, finish_email_scan, prepare_email_batch
import marketplace_web
from marketplace_web import public_listing, public_scan, result_path, scan_stage_progress


class MarketplaceWebTests(unittest.TestCase):
    def test_public_admin_requires_verified_email_with_admin_role(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
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
                        connection.request("GET", "/Facebooknotify" + path,
                                           headers=headers)
                        response = connection.getresponse()
                        body = response.read()
                        result = response.status, (json.loads(body)
                                                   if response.getheader("Content-Type", "").startswith("application/json")
                                                   else body.decode("utf-8"))
                        connection.close()
                        return result

                    self.assertEqual(request("/api/admin")[0], 401)
                    self.assertEqual(request("/api/admin", "member@example.com",
                                             spoof="admin@example.com")[0], 403)
                    self.assertEqual(request("/admin", "member@example.com")[0], 403)
                    status, overview = request("/api/admin", "admin@example.com")
                    self.assertEqual(status, 200)
                    self.assertEqual(overview["admin_emails"], ["admin@example.com"])
                    self.assertEqual(request("/admin", "admin@example.com")[0], 200)
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
                                           "/Facebooknotify" + path,
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

                status, html = request("/Facebooknotify/", "scout.example.com")
                self.assertEqual(status, 200)
                self.assertIn('href="/Facebooknotify/styles.css"', html)
                self.assertIn('data-public-writes="true"', html)
                self.assertEqual(request("/", "scout.example.com")[0], 403)
                self.assertEqual(request("/Facebooknotify/api/admin",
                                         "scout.example.com")[0], 403)
                self.assertEqual(request("/Facebooknotify/admin",
                                         "scout.example.com")[0], 403)
                self.assertEqual(request("/Facebooknotify/api/scans",
                                         "scout.example.com")[0], 200)
                self.assertEqual(request("/Facebooknotify/api/account",
                                         "scout.example.com",
                                         "https://another.example.com")[0], 403)
                self.assertEqual(request("/Facebooknotify/app.js",
                                         "scout.example.com")[0], 200)
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

                status, html = request("GET", "/Facebooknotify/")
                self.assertEqual(status, 200)
                self.assertIn('data-public-writes="false"', html)
                self.assertEqual(request("GET", "/Facebooknotify/api/account")[0], 403)
                self.assertEqual(request("POST", "/Facebooknotify/api/scans")[0], 403)
                self.assertEqual(request("DELETE", "/Facebooknotify/api/searches/1")[0], 403)
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
                         "AI photo classification timed out")
        self.assertEqual(result["ai_error_count"], 1)

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
                               return_value={"result_count": 0}) as detail):
                scans = marketplace_web.scan_manifests()
        by_id = {scan["id"]: scan for scan in scans}
        self.assertEqual(by_id["scan-complete"]["result_count"], 0)
        self.assertEqual(by_id["scan-complete"]["within_radius_count"], 3)
        self.assertNotIn("result_count", by_id["scan-running"])
        detail.assert_called_once()

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
                email_batch_size=2)
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
        with patch.object(marketplace_web, "pending_slots", return_value=slots):
            queue = marketplace_web.account_scan_queue(
                {"configured": True, "id": "demo"}, scans)
        self.assertEqual([row["keywords"] for row in queue],
                         [["free"], ["scooter"], ["mower"]])
        self.assertEqual([row["status"] for row in queue],
                         ["running", "queued", "queued"])
        self.assertEqual([row["id"] for row in queue], ["scan-free", None, None])


if __name__ == "__main__":
    unittest.main()
