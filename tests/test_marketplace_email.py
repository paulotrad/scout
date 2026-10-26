"""Email digests go out only for new matches after every keyword finishes."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from marketplace_alerts import record_scan_matches, save_account
from marketplace_email import (
    deliver_pending_summaries, email_status, finish_email_scan,
    prepare_email_batch,
)


class MarketplaceEmailTests(unittest.TestCase):
    def test_empty_batch_creates_no_email(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"SCOUT_PUBLIC_HOST": "scout.example.com"}, clear=True):
            db = Path(directory) / "alerts.sqlite"
            account = save_account({"email": "buyer@example.com",
                                    "zip_code": "21009", "radius_miles": 15,
                                    "terms": ["chair"], "plan": "free",
                                    "timezone": "America/New_York"}, db)
            self.assertTrue(prepare_email_batch(account["id"], "run-empty", 1, db))
            scan = {"id": "scan-empty", "account_id": account["id"],
                    "email_batch_key": "run-empty", "status": "complete",
                    "keywords": ["chair"], "zip_code": "21009",
                    "radius_miles": 15, "listings": []}
            self.assertEqual(record_scan_matches(scan, db), 0)
            self.assertFalse(finish_email_scan(scan, db))
            self.assertEqual(email_status(db)["pending"], 0)

    def test_multiple_keyword_matches_become_one_retryable_email(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"SCOUT_PUBLIC_HOST": "scout.example.com"}, clear=True):
            db = Path(directory) / "alerts.sqlite"
            account = save_account({"email": "buyer@example.com",
                                    "zip_code": "21009", "radius_miles": 15,
                                    "terms": ["chair", "desk"], "plan": "free",
                                    "timezone": "America/New_York"}, db)
            self.assertTrue(prepare_email_batch(account["id"], "run-two", 2, db))

            def scan(scan_id, keyword, price):
                return {"id": scan_id, "account_id": account["id"],
                        "email_batch_key": "run-two", "status": "complete",
                        "keywords": [keyword], "zip_code": "21009",
                        "radius_miles": 15, "listings": [{
                            "id": scan_id + "-item", "title": keyword.title(),
                            "item_type": keyword, "listing_kind": "product",
                            "condition_grade": "good", "location": "Edgewood, MD",
                            "distance_miles_approx": 2, "asking_price": price}]}

            first = scan("scan-chair", "chair", 0)
            second = scan("scan-desk", "desk", 20)
            self.assertEqual(record_scan_matches(first, db), 1)
            self.assertFalse(finish_email_scan(first, db))
            self.assertEqual(email_status(db)["pending"], 0)
            self.assertEqual(record_scan_matches(second, db), 1)
            self.assertTrue(finish_email_scan(second, db))
            self.assertEqual(email_status(db)["pending"], 1)
            sent = []
            self.assertEqual(deliver_pending_summaries(db,
                sender=lambda recipient, subject, body: sent.append(
                    (recipient, subject, body))), 1)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], "buyer@example.com")
            self.assertIn("2 new matching items", sent[0][1])
            self.assertIn("Chair", sent[0][2])
            self.assertIn("Desk", sent[0][2])
            self.assertIn("FREE", sent[0][2])
            self.assertIn("$20.00", sent[0][2])
            self.assertIn("https://scout.example.com/Facebooknotify/scan/scan-chair#item-",
                          sent[0][2])
            self.assertEqual(email_status(db)["sent"], 1)
            self.assertFalse(finish_email_scan(second, db))
            self.assertEqual(deliver_pending_summaries(db,
                sender=lambda *_: sent.append("duplicate")), 0)

    def test_failed_keyword_does_not_block_a_matching_summary(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"SCOUT_PUBLIC_HOST": "scout.example.com"}, clear=True):
            db = Path(directory) / "alerts.sqlite"
            account = save_account({"email": "buyer@example.com",
                                    "zip_code": "21009", "radius_miles": 15,
                                    "terms": ["chair", "desk"], "plan": "free",
                                    "timezone": "America/New_York"}, db)
            prepare_email_batch(account["id"], "run-failed", 2, db)
            found = {"id": "scan-found", "account_id": account["id"],
                     "email_batch_key": "run-failed", "status": "complete",
                     "keywords": ["chair"], "zip_code": "21009",
                     "radius_miles": 15, "listings": [{
                         "id": "chair-1", "title": "Chair", "item_type": "chair",
                         "listing_kind": "product", "condition_grade": "good",
                         "distance_miles_approx": 2, "asking_price": 0}]}
            record_scan_matches(found, db)
            self.assertFalse(finish_email_scan(found, db))
            self.assertTrue(finish_email_scan({"id": "scan-failed",
                "account_id": account["id"], "email_batch_key": "run-failed",
                "status": "failed", "keywords": ["desk"]}, db))
            self.assertEqual(email_status(db)["pending"], 1)

    def test_disabling_email_clears_unsent_summary_and_stops_future_batches(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"SCOUT_PUBLIC_HOST": "scout.example.com"}, clear=True):
            db = Path(directory) / "alerts.sqlite"
            account = save_account({"email": "buyer@example.com",
                                    "zip_code": "21009", "radius_miles": 15,
                                    "terms": ["chair"], "plan": "free",
                                    "timezone": "America/New_York"}, db)
            self.assertTrue(prepare_email_batch(account["id"], "run-before-off", 1, db))
            scan = {"id": "scan-before-off", "account_id": account["id"],
                    "email_batch_key": "run-before-off", "status": "complete",
                    "keywords": ["chair"], "zip_code": "21009", "radius_miles": 15,
                    "listings": [{"id": "chair-new", "title": "Chair", "item_type": "chair",
                                  "listing_kind": "product", "condition_grade": "good",
                                  "distance_miles_approx": 2, "asking_price": 0}]}
            self.assertEqual(record_scan_matches(scan, db), 1)
            self.assertTrue(finish_email_scan(scan, db))
            self.assertEqual(email_status(db)["pending"], 1)
            disabled = save_account({**account, "email_notifications_enabled": False}, db)
            self.assertFalse(disabled["email_notifications_enabled"])
            self.assertEqual(email_status(db)["pending"], 0)
            self.assertFalse(prepare_email_batch(account["id"], "run-after-off", 1, db))
            sent = []
            self.assertEqual(deliver_pending_summaries(db, sender=lambda *_: sent.append(1)), 0)
            self.assertEqual(sent, [])
            enabled = save_account({**disabled, "email_notifications_enabled": True}, db)
            self.assertTrue(enabled["email_notifications_enabled"])
            self.assertTrue(prepare_email_batch(account["id"], "run-after-on", 1, db))
            self.assertEqual(email_status(db)["pending"], 0)


if __name__ == "__main__":
    unittest.main()
