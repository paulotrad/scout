"""Saved searches should alert on relevant items once, without service ads."""

from pathlib import Path
from datetime import datetime, timezone
import tempfile
import unittest

from marketplace_alerts import (
    claim_slot, create_search, delete_search, due_slot, finish_slot,
    get_account, get_demo_account, item_matches, list_account_profiles,
    list_alerts, list_searches,
    mark_read, next_scheduled_at, open_account, pending_slots, record_scan_matches, release_slot,
    save_account, save_demo_account, validate_search,
)


class MarketplaceAlertTests(unittest.TestCase):
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
            self.assertTrue(claim_slot("2026-10-23-07:item:desk", db, "account", item))
            slots = pending_slots(db)
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0]["account_id"], "account")
            self.assertEqual(slots[0]["slot_key"], "2026-10-23-07:item:desk")
            self.assertEqual(slots[0]["watch_item"], item)
            self.assertTrue(slots[0]["claimed_at"])

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
            self.assertEqual(list_alerts(db, search_id=first["search_id"],
                                         scan_ids=set()),
                             {"unread_count": 0, "alerts": []})

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


if __name__ == "__main__":
    unittest.main()
