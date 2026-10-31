"""Saved searches should alert on relevant items once, without service ads."""

from pathlib import Path
from datetime import datetime, timezone
import tempfile
import unittest
import sqlite3
from contextlib import closing

from marketplace_alerts import (
    claim_slot, create_search, delete_search, due_fast_slot, due_slot, finish_slot,
    get_account, get_demo_account, grant_admin, hide_all_inbox_alerts,
    is_admin_email, item_matches,
    list_account_profiles, list_admin_emails, list_alerts, list_searches,
    mark_read, next_fast_at, next_scheduled_at, open_account, pending_slots,
    record_scan_matches, release_slot, remove_nonproduct_alerts,
    revoke_admin, save_account, save_demo_account, set_alert_triage,
    validate_search,
)


class MarketplaceAlertTests(unittest.TestCase):
    def test_service_guard_rejects_product_model_label_and_cleans_old_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "alerts.sqlite"
            search = create_search({"zip_code": "21009", "radius_miles": 15,
                                    "scan_keywords": ["shed"],
                                    "item_terms": ["shed"],
                                    "minimum_grade": "any"}, db_path)
            scan = {"id": "scan-sheds", "status": "complete", "zip_code": "21009",
                    "radius_miles": 15}
            service = {"id": "1", "title": "Need shed built?",
                       "description": "Shed building", "item_type": "shed",
                       "listing_kind": "product", "distance_miles_approx": 3,
                       "asking_price": 100}
            self.assertFalse(item_matches(search, scan, service))
            genuine = {**service, "id": "2", "title": "Used shed for sale",
                       "description": "Asking $400. Delivery available.",
                       "asking_price": 400}
            self.assertTrue(item_matches(search, scan, genuine))
            titled_price = {**genuine, "asking_price": 25,
                            "price_source": "seller_title",
                            "review_flags": ["zero_price_card_may_not_be_free"]}
            self.assertTrue(item_matches(search, scan, titled_price))
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE listings (source TEXT, listing_id TEXT, "
                           "title TEXT, description TEXT)")
                db.execute("INSERT INTO listings VALUES (?,?,?,?)",
                           ("facebook_marketplace", "1", service["title"],
                            service["description"]))
                db.execute("INSERT INTO match_alerts "
                           "(search_id, scan_id, listing_id, title, item_type, "
                           "condition_grade, matched_at) VALUES (?,?,?,?,?,?,?)",
                           (search["id"], scan["id"], "1", service["title"],
                            "shed", "unknown", datetime.now(timezone.utc).isoformat()))
            self.assertEqual(remove_nonproduct_alerts(db_path), 1)
            self.assertEqual(list_alerts(db_path, search_id=search["id"])["alerts"], [])

    def test_hide_all_moves_only_current_inbox_and_future_matches_stay_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            settings = {"zip_code": "21009", "radius_miles": 15,
                        "scan_keywords": ["chair"], "item_terms": ["chair"],
                        "minimum_grade": "any"}
            first = create_search(settings, db)
            second = create_search(settings, db)
            def scan(scan_id, listing_id):
                return {"id": scan_id, "status": "complete", "zip_code": "21009",
                        "radius_miles": 15, "listings": [
                            {"id": listing_id, "title": "Wood chair",
                             "item_type": "chair", "listing_kind": "product",
                             "distance_miles_approx": 4, "asking_price": 20}]}
            record_scan_matches(scan("scan-one", "chair-one"), db)
            record_scan_matches(scan("scan-two", "chair-two"), db)
            first_alerts = list_alerts(db, search_id=first["id"])["alerts"]
            self.assertTrue(set_alert_triage(first_alerts[0]["id"], "saved", db,
                                             search_id=first["id"]))
            self.assertEqual(hide_all_inbox_alerts(first["id"], db), 1)
            self.assertEqual(hide_all_inbox_alerts(first["id"], db), 0)
            first_counts = list_alerts(db, search_id=first["id"])["triage_counts"]
            self.assertEqual(first_counts, {"inbox": 0, "saved": 1, "hidden": 1})
            self.assertEqual(list_alerts(db, search_id=first["id"])["unread_count"], 0)
            self.assertEqual(list_alerts(db, search_id=second["id"])
                             ["triage_counts"]["inbox"], 2)
            hidden = list_alerts(db, search_id=first["id"],
                                 triage_status="hidden")["alerts"][0]
            self.assertTrue(set_alert_triage(hidden["id"], "inbox", db,
                                             search_id=first["id"]))
            self.assertEqual(list_alerts(db, search_id=first["id"])
                             ["triage_counts"]["inbox"], 1)
            hide_all_inbox_alerts(first["id"], db)
            record_scan_matches(scan("scan-three", "chair-three"), db)
            self.assertEqual(list_alerts(db, search_id=first["id"])
                             ["triage_counts"]["inbox"], 1)

    def test_alert_pages_keep_older_matches_reachable(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            search = create_search({"zip_code": "21009", "radius_miles": 15,
                                    "scan_keywords": ["free"],
                                    "item_terms": ["free"],
                                    "minimum_grade": "any"}, db)
            self.assertEqual(record_scan_matches({
                "id": "scan-pages", "status": "complete", "zip_code": "21009",
                "radius_miles": 15,
                "listings": [{"id": str(number), "title": f"Chair {number}",
                              "item_type": "chair", "listing_kind": "product",
                              "distance_miles_approx": 3, "asking_price": 0}
                             for number in range(3)]}, db), 3)
            first_page = list_alerts(db, limit=2, search_id=search["id"])
            second_page = list_alerts(db, limit=2, offset=2,
                                      search_id=search["id"])
            self.assertEqual(first_page["triage_counts"]["inbox"], 3)
            self.assertEqual(len(first_page["alerts"]), 2)
            self.assertEqual(len(second_page["alerts"]), 1)
            self.assertFalse({row["id"] for row in first_page["alerts"]} &
                             {row["id"] for row in second_page["alerts"]})
            with self.assertRaises(ValueError):
                list_alerts(db, offset=-1)

    def test_alert_triage_is_saved_and_owned_by_the_search(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            settings = {"zip_code": "21009", "radius_miles": 15,
                        "scan_keywords": ["chair"], "item_terms": ["chair"],
                        "minimum_grade": "any"}
            first = create_search(settings, db)
            other = create_search(settings, db)
            scan = {"id": "scan-triage", "status": "complete",
                    "zip_code": "21009", "radius_miles": 15,
                    "listings": [{"id": "chair", "title": "Wood chair",
                                  "item_type": "chair", "listing_kind": "product",
                                  "distance_miles_approx": 4,
                                  "asking_price": 20}]}
            self.assertEqual(record_scan_matches(scan, db), 2)
            first_alert = list_alerts(db, search_id=first["id"])["alerts"][0]
            self.assertFalse(set_alert_triage(first_alert["id"], "saved", db,
                                              search_id=other["id"]))
            self.assertTrue(set_alert_triage(first_alert["id"], "saved", db,
                                             search_id=first["id"]))
            inbox = list_alerts(db, search_id=first["id"])
            saved = list_alerts(db, search_id=first["id"], triage_status="saved")
            self.assertEqual(inbox["alerts"], [])
            self.assertEqual(inbox["unread_count"], 0)
            self.assertEqual(saved["alerts"][0]["triage_status"], "saved")
            self.assertIsNotNone(saved["alerts"][0]["read_at"])
            self.assertEqual(saved["triage_counts"]["saved"], 1)
            self.assertEqual(list_alerts(db, search_id=other["id"])["triage_counts"]["saved"], 0)
            with self.assertRaises(ValueError):
                set_alert_triage(first_alert["id"], "deleted", db,
                                 search_id=first["id"])

    def test_admin_roles_are_persisted_and_case_insensitive(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            self.assertFalse(is_admin_email("person@example.com", db))
            grant_admin(" Person@Example.com ", db)
            grant_admin("person@example.com", db)
            self.assertTrue(is_admin_email("PERSON@example.com", db))
            self.assertEqual(list_admin_emails(db), ["person@example.com"])
            self.assertTrue(revoke_admin("PERSON@example.com", db))
            self.assertFalse(is_admin_email("person@example.com", db))
            self.assertFalse(revoke_admin("person@example.com", db))
            with self.assertRaises(ValueError):
                grant_admin("not-an-email", db)

    def test_validates_search_settings(self):
        settings = validate_search({
            "zip_code": "21009", "radius_miles": 15,
            "scan_keywords": ["free"], "item_terms": ["Couch", "couch", "sofa"],
            "minimum_grade": "good",
        })
        self.assertEqual(settings["item_terms"], ["Couch", "sofa"])
        for change in ({"minimum_grade": "perfect"},
                       {"item_terms": ["bad\nterm"]},
                       {"radius_miles": 0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_search({**settings, **change})

    def test_matches_item_terms_condition_and_radius(self):
        search = {"zip_code": "21009", "radius_miles": 15,
                  "item_terms": ["sofa", "couch"], "minimum_grade": "good"}
        scan = {"zip_code": "21009", "radius_miles": 20}
        item = {"listing_kind": "product", "title": "Free",
                "item_type": "couch", "condition_grade": "good",
                "distance_miles_approx": 12.5, "asking_price": 0}
        self.assertTrue(item_matches(search, scan, item))
        self.assertFalse(item_matches(search, scan, {**item,
                         "listing_kind": "service"}))
        self.assertFalse(item_matches(search, scan, {**item,
                         "condition_grade": "unknown"}))
        self.assertFalse(item_matches(search, scan, {**item,
                         "distance_miles_approx": 16}))
        self.assertFalse(item_matches(search, {**scan, "zip_code": "21010"}, item))
        self.assertFalse(item_matches(search, {**scan, "radius_miles": 10}, item))

    def test_pet_listing_matches_without_a_condition_grade(self):
        search = {"zip_code": "21009", "radius_miles": 15,
                  "item_terms": ["dog"], "minimum_grade": "good"}
        scan = {"zip_code": "21009", "radius_miles": 15}
        item = {"listing_kind": "product", "pet_listing": True,
                "title": "Dog to Good Home", "item_type": "dog",
                "condition_grade": "", "distance_miles_approx": 4,
                "asking_price": 0}
        self.assertTrue(item_matches(search, scan, item))

    def test_alerts_are_deduplicated_across_scans_and_marked_read(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            watch = create_search({
                "zip_code": "21009", "radius_miles": 15,
                "scan_keywords": ["free"], "item_terms": ["couch"],
                "minimum_grade": "fair",
            }, db)
            self.assertEqual(len(list_searches(db)), 1)
            scan = {"id": "scan-a", "status": "complete", "zip_code": "21009",
                    "radius_miles": 15, "listings": [
                        {"id": "123", "title": "Free couch", "item_type": "couch",
                         "listing_kind": "product", "condition_grade": "good",
                         "distance_miles_approx": 3, "asking_price": 0},
                        {"id": "456", "title": "Free pickup of couches",
                         "item_type": "", "listing_kind": "service",
                         "condition_grade": "unknown", "distance_miles_approx": 4,
                         "asking_price": 0},
                    ]}
            self.assertEqual(record_scan_matches(scan, db), 1)
            self.assertEqual(record_scan_matches({**scan, "id": "scan-b"}, db), 0)
            alerts = list_alerts(db)
            self.assertEqual(alerts["unread_count"], 1)
            self.assertEqual(len(alerts["alerts"]), 1)
            self.assertTrue(mark_read(alerts["alerts"][0]["id"], db))
            self.assertEqual(list_alerts(db)["unread_count"], 0)
            self.assertTrue(delete_search(watch["id"], db))
            self.assertEqual(list_alerts(db)["alerts"], [])

    def test_demo_plan_saves_25_terms_and_price_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            self.assertFalse(get_demo_account(db)["configured"])
            profile = save_demo_account({
                "zip_code": "21009", "radius_miles": 15,
                "terms": [f"item {i}" for i in range(25)],
                "minimum_grade": "good", "min_price": 50,
                "max_price": 500, "plan": "plus_preview",
                "timezone": "America/New_York",
            }, db)
            self.assertEqual(len(profile["terms"]), 25)
            self.assertEqual(profile["plan"], "plus_preview")
            self.assertEqual(len(list_searches(db)), 1)
            watch = list_searches(db)[0]
            item = {"listing_kind": "product", "title": "item 1",
                    "item_type": "chair", "condition_grade": "good",
                    "distance_miles_approx": 5, "asking_price": 40}
            self.assertFalse(item_matches(watch, profile, item))
            self.assertTrue(item_matches(watch, profile,
                                         {**item, "asking_price": 200}))
            self.assertFalse(item_matches(watch, profile,
                         {**item, "asking_price": 200,
                          "review_flags": ["zero_price_card_may_not_be_free"]}))
            self.assertTrue(item_matches(watch, profile,
                         {**item, "asking_price": 200,
                          "price_source": "seller_description",
                          "review_flags": ["zero_price_card_may_not_be_free"]}))
            updated = save_demo_account({
                "zip_code": "21009", "radius_miles": 15,
                "terms": ["desk"], "minimum_grade": "any",
                "min_price": 0, "max_price": 250,
                "plan": "free", "timezone": "America/Chicago",
            }, db)
            self.assertEqual(updated["terms"], ["desk"])
            self.assertEqual(updated["timezone"], "America/Chicago")
            self.assertEqual(len(list_searches(db)), 1)
            with self.assertRaises(ValueError):
                save_demo_account({**profile, "terms": [str(i) for i in range(26)]}, db)

    def test_email_claims_legacy_demo_and_separates_new_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            settings = {"zip_code": "21009", "radius_miles": 15,
                        "terms": ["free", "chair"], "plan": "free",
                        "timezone": "America/New_York"}
            legacy = save_demo_account(settings, db)
            first = save_account({**settings, "email": " First@Example.com "}, db)
            self.assertEqual(first["id"], "demo")
            self.assertEqual(first["search_id"], legacy["search_id"])
            self.assertEqual(first["email"], "first@example.com")
            self.assertTrue(get_account("FIRST@example.com", db)["configured"])
            second = save_account({**settings, "email": "second@example.com",
                                   "zip_code": "10001", "plan": "plus_preview"}, db)
            self.assertNotEqual(second["id"], first["id"])
            self.assertEqual(get_account("first@example.com", db)["zip_code"], "21009")
            self.assertEqual(get_account("second@example.com", db)["zip_code"], "10001")
            self.assertEqual(len(list_account_profiles(db)), 2)
            with self.assertRaises(ValueError):
                save_account({**settings, "email": "invalid email"}, db)

    def test_verified_public_account_cannot_claim_unowned_legacy_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            legacy = save_demo_account({"zip_code": "21009", "radius_miles": 15,
                                        "terms": ["private keyword"], "plan": "free",
                                        "timezone": "America/New_York"}, db)
            profile = open_account("new@example.com", db,
                                   allow_legacy_claim=False)
            self.assertNotEqual(profile["id"], legacy["id"])
            self.assertEqual(profile["terms"], ["free"])
            self.assertEqual(get_account(None, db)["terms"], ["private keyword"])

    def test_open_email_profile_is_saved_and_keeps_its_watchlist(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            first = open_account(" First@Example.com ", db)
            self.assertEqual(first["email"], "first@example.com")
            self.assertTrue(first["configured"])
            self.assertEqual(len(list_account_profiles(db)), 1)
            updated = save_account({**first, "terms": ["couch"],
                                    "watch_items": [{"term": "couch", "zip_code": "21009",
                                                     "radius_miles": 15, "min_price": 0,
                                                     "max_price": 1000}]}, db)
            reopened = open_account("FIRST@example.com", db)
            self.assertEqual(reopened["id"], updated["id"])
            self.assertEqual(reopened["terms"], ["couch"])
            second = open_account("second@example.com", db)
            self.assertNotEqual(second["id"], first["id"])
            self.assertEqual(second["terms"], ["free"])
            self.assertEqual(len(list_account_profiles(db)), 2)

    def test_saving_new_keywords_removes_only_obsolete_waiting_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            scooter = {"term": "electric scooter", "zip_code": "21009",
                       "radius_miles": 30, "min_price": 0, "max_price": 30,
                       "fast": False}
            shed = {**scooter, "term": "shed", "max_price": 100}
            account = save_account({"email": "buyer@example.com", "plan": "plus_preview",
                                    "timezone": "America/New_York",
                                    "zip_code": "21009", "radius_miles": 30,
                                    "terms": ["electric scooter", "shed"],
                                    "watch_items": [scooter, shed]}, db)
            self.assertTrue(claim_slot("retry:scooter:item:old", db,
                                       account_id=account["id"], watch_item=scooter,
                                       scan_mode="scheduled"))
            self.assertTrue(claim_slot("today:item:shed", db,
                                       account_id=account["id"], watch_item=shed,
                                       scan_mode="scheduled"))
            save_account({**account, "terms": ["shed"],
                          "watch_items": [shed]}, db)
            self.assertEqual([row["slot_key"] for row in pending_slots(db)],
                             ["today:item:shed"])

    def test_email_notification_setting_is_per_profile_and_survives_other_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            first = open_account("first@example.com", db)
            second = open_account("second@example.com", db)
            self.assertTrue(first["email_notifications_enabled"])
            disabled = save_account({**first, "email_notifications_enabled": False}, db)
            self.assertFalse(disabled["email_notifications_enabled"])
            edited = save_account({key: value for key, value in disabled.items()
                                   if key != "email_notifications_enabled"}, db)
            self.assertFalse(edited["email_notifications_enabled"])
            self.assertFalse(get_account("first@example.com", db)["email_notifications_enabled"])
            self.assertTrue(get_account("second@example.com", db)["email_notifications_enabled"])
            self.assertTrue(next(profile for profile in list_account_profiles(db)
                                 if profile["id"] == second["id"])["email_notifications_enabled"])
            with self.assertRaises(ValueError):
                save_account({**edited, "email_notifications_enabled": "false"}, db)

    def test_per_keyword_settings_and_global_locks_survive_save(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            payload = {
                "email": "buyer@example.com", "zip_code": "21009",
                "radius_miles": 15, "min_price": 0, "max_price": 500,
                "terms": ["couch", "desk"], "plan": "free",
                "timezone": "America/New_York",
                "global_locks": {"zip_code": True, "radius_miles": False,
                                 "min_price": False, "max_price": False},
                "watch_items": [
                    {"term": "couch", "zip_code": "10001", "radius_miles": 10,
                     "min_price": 0, "max_price": 100},
                    {"term": "desk", "zip_code": "10001", "radius_miles": 30,
                     "min_price": 50, "max_price": 300},
                ],
            }
            account = save_account(payload, db)
            self.assertEqual(account["watch_items"][0]["zip_code"], "21009")
            self.assertEqual(account["watch_items"][1]["radius_miles"], 30)
            self.assertEqual(get_account("buyer@example.com", db)["watch_items"],
                             account["watch_items"])
            search = list_searches(db)[0]
            scan = {"zip_code": "21009", "radius_miles": 10,
                    "watch_term": "couch"}
            item = {"listing_kind": "product", "title": "Couch",
                    "item_type": "couch", "condition_grade": "good",
                    "distance_miles_approx": 8, "asking_price": 80}
            self.assertTrue(item_matches(search, scan, item))
            self.assertFalse(item_matches(search, scan, {**item, "asking_price": 150}))
            self.assertFalse(item_matches(search, {**scan, "watch_term": "desk"}, item))
            self.assertFalse(item_matches(search, scan, {**item,
                             "distance_miles_approx": 12}))
            with self.assertRaises(ValueError):
                save_account({**payload, "watch_items": [
                    {**payload["watch_items"][0], "radius_miles": 0},
                    payload["watch_items"][1]]}, db)

    def test_pending_slot_keeps_item_settings_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            item = {"term": "desk", "zip_code": "10001", "radius_miles": 5,
                    "min_price": 20, "max_price": 80}
            self.assertTrue(claim_slot("2026-10-23-07:item:desk", db, "account", item,
                                       not_before="2026-10-23T11:05:00+00:00",
                                       scan_mode="incremental", retry_root="scan-old",
                                       retry_attempt=1))
            slots = pending_slots(db)
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0]["account_id"], "account")
            self.assertEqual(slots[0]["slot_key"], "2026-10-23-07:item:desk")
            self.assertEqual(slots[0]["watch_item"], item)
            self.assertTrue(slots[0]["claimed_at"])
            self.assertEqual(slots[0]["not_before"], "2026-10-23T11:05:00+00:00")
            self.assertEqual(slots[0]["scan_mode"], "incremental")
            self.assertEqual(slots[0]["retry_root"], "scan-old")
            self.assertEqual(slots[0]["retry_attempt"], 1)

    def test_owned_scan_alerts_only_its_email_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            settings = {"zip_code": "21009", "radius_miles": 15,
                        "terms": ["chair"], "plan": "free",
                        "timezone": "America/New_York"}
            first = save_account({**settings, "email": "first@example.com"}, db)
            second = save_account({**settings, "email": "second@example.com"}, db)
            scan = {"id": "scan-owned", "account_id": first["id"],
                    "status": "complete", "zip_code": "21009",
                    "radius_miles": 15, "listings": [{
                        "id": "chair-1", "title": "Wood chair", "item_type": "chair",
                        "listing_kind": "product", "condition_grade": "good",
                        "distance_miles_approx": 3, "asking_price": 0}]}
            self.assertEqual(record_scan_matches(scan, db), 1)
            self.assertEqual(len(list_alerts(db, search_id=first["search_id"])["alerts"]), 1)
            self.assertEqual(list_alerts(db, search_id=second["search_id"])["alerts"], [])
            self.assertEqual(len(list_alerts(db, search_id=first["search_id"],
                                             scan_ids={"scan-owned"})["alerts"]), 1)
            empty = list_alerts(db, search_id=first["search_id"], scan_ids=set())
            self.assertEqual(empty["unread_count"], 0)
            self.assertEqual(empty["alerts"], [])

    def test_same_schedule_slot_is_claimed_per_account(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            self.assertTrue(claim_slot("2026-10-23-07", db, "first"))
            self.assertTrue(claim_slot("2026-10-23-07", db, "second"))
            self.assertFalse(claim_slot("2026-10-23-07", db, "first"))
            self.assertEqual(len(pending_slots(db)), 2)
            finish_slot("2026-10-23-07", "scan-first", db, "first")
            slots = pending_slots(db)
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0]["account_id"], "second")
            self.assertEqual(slots[0]["slot_key"], "2026-10-23-07")

    def test_daily_schedule_uses_selected_local_timezone_and_noon_plus_slot(self):
        account = {"configured": True, "plan": "plus_preview",
                   "timezone": "America/New_York"}
        morning = datetime(2026, 10, 23, 10, 59, tzinfo=timezone.utc)
        self.assertIsNone(due_slot(account, morning))
        self.assertEqual(due_slot(account, datetime(2026, 10, 23, 11, 1,
                                                  tzinfo=timezone.utc)),
                         "2026-10-23-07")
        self.assertEqual(due_slot(account, datetime(2026, 10, 23, 12, 30,
                                                  tzinfo=timezone.utc)),
                         "2026-10-23-07")
        self.assertIsNone(due_slot(account, datetime(2026, 10, 23, 13, 1,
                                                  tzinfo=timezone.utc)))
        self.assertEqual(due_slot(account, datetime(2026, 10, 23, 16, 1,
                                                  tzinfo=timezone.utc)),
                         "2026-10-23-12")
        self.assertEqual(next_scheduled_at(account, morning).hour, 7)
        account["plan"] = "free"
        self.assertIsNone(due_slot(account, datetime(2026, 10, 23, 16, 1,
                                                  tzinfo=timezone.utc)))
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            self.assertTrue(claim_slot("2026-10-23-07", db))
            self.assertFalse(claim_slot("2026-10-23-07", db))
            finish_slot("2026-10-23-07", "scan-test", db)
            release_slot("2026-10-23-07", db)
            self.assertFalse(claim_slot("2026-10-23-07", db))

    def test_plus_priority_keyword_gets_bounded_half_hour_checks(self):
        watch = [{"term": "mower", "zip_code": "21009", "radius_miles": 15,
                  "min_price": 0, "max_price": 100, "fast": True},
                 {"term": "chair", "zip_code": "21009", "radius_miles": 15,
                  "min_price": 0, "max_price": 100, "fast": False}]
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "alerts.sqlite"
            account = save_account({"email": "buyer@example.com", "zip_code": "21009",
                                    "radius_miles": 15, "terms": ["mower", "chair"],
                                    "plan": "plus_preview", "timezone": "America/New_York",
                                    "watch_items": watch}, db)
            self.assertTrue(account["watch_items"][0]["fast"])
            self.assertEqual(next_fast_at(account, datetime(
                2026, 10, 23, 13, 40, tzinfo=timezone.utc)).hour, 10)
            self.assertEqual(next_fast_at(account, datetime(
                2026, 10, 23, 13, 40, tzinfo=timezone.utc)).minute, 0)
            self.assertEqual(next_fast_at(account, datetime(
                2026, 10, 23, 10, 45, tzinfo=timezone.utc)).hour, 8)
            self.assertEqual(due_fast_slot(account, datetime(
                2026, 10, 23, 14, 30, tzinfo=timezone.utc)),
                "fast-2026-10-23-10-30")
            self.assertEqual(due_fast_slot(account, datetime(
                2026, 10, 23, 14, 6, tzinfo=timezone.utc)),
                "fast-2026-10-23-10-00")
            self.assertIsNone(due_fast_slot(account, datetime(
                2026, 10, 23, 14, 25, tzinfo=timezone.utc)))
            self.assertIsNone(due_fast_slot(account, datetime(
                2026, 10, 23, 11, 30, tzinfo=timezone.utc)))
            with self.assertRaises(ValueError):
                save_account({**account, "plan": "free"}, db)
            with self.assertRaises(ValueError):
                save_account({**account, "watch_items": [
                    watch[0], {**watch[1], "fast": True}]}, db)


if __name__ == "__main__":
    unittest.main()
