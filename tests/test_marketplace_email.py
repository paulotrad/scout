"""Email digests go out only for new matches after every keyword finishes."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from marketplace_alerts import record_scan_matches, save_account
from marketplace_email import (
    _build_message, _summary_html, _summary_text, alert_item_summary,
    deliver_pending_summaries, email_status, finish_email_scan,
    prepare_email_batch,
)
from marketplace_web import public_listing


class MarketplaceEmailTests(unittest.TestCase):
    def test_email_explains_match_even_without_price_comparisons(self):
        titled = alert_item_summary({
            "id": "1", "title": "Electric scooter", "asking_price": 25,
            "distance_miles_approx": 4.2}, "scooter")
        self.assertIn("Seller title matches your scooter search", titled["reason"])
        self.assertIn("4.2 mi away", titled["reason"])
        described = alert_item_summary({
            "id": "2", "title": "Outdoor tool", "description": "Battery mower",
            "asking_price": 40}, "mower")
        self.assertIn("Seller description matches", described["reason"])
        classified = alert_item_summary({
            "id": "3", "title": "Yard equipment", "item_type": "lawn_mower",
            "asking_price": 40}, "mower")
        self.assertIn("Scout identified an item", classified["reason"])
        free = alert_item_summary({
            "id": "4", "title": "Chair", "asking_price": 0}, "free")
        self.assertIn("Facebook listed it as free", free["reason"])
        rows = [{"scan_id": "scan", "keyword": "mower", "items": [described]}]
        _, plain = _summary_text(rows, "https://example.com/notify")
        self.assertIn("Why this matched: Seller description", plain)

    def test_email_card_uses_short_oil_name(self):
        item = public_listing("scan-oil", {
            "listing_id": "1153599817617933", "asking_price": 0,
            "title": "Winter is coming! 32 gal. Of waste oil from automotive "
                     "oil changes Free Free Free all in gal. Jugs",
            "description": "Free come get it .",
            "vision": {"observations": {"listing_kind": "product",
                                        "item_type": "waste oil"}},
        })
        summary = alert_item_summary(item, "free")
        rows = [{"scan_id": "scan-oil", "keyword": "free", "items": [summary]}]
        subject, plain = _summary_text(rows, "https://example.com/notify")
        html = _summary_html(rows, "https://example.com/notify")
        self.assertIn("Used automotive oil in jugs", subject)
        self.assertIn("Used automotive oil in jugs", html)
        self.assertNotIn("Winter is coming", html)
        self.assertNotIn("Winter is coming", plain)

    def test_pet_email_keeps_listing_without_condition_claim(self):
        item = public_listing("scan-pet", {
            "listing_id": "12345", "asking_price": 0,
            "title": "Dog to Good Home", "description": "Giving a dog away.",
            "vision": {"observations": {"listing_kind": "product",
                                        "item_type": "dog"}},
        })
        summary = alert_item_summary(item, "dog")
        rows = [{"scan_id": "scan-pet", "keyword": "dog", "items": [summary]}]
        _, plain = _summary_text(rows, "https://example.com/notify")
        html = _summary_html(rows, "https://example.com/notify")
        self.assertIn("Dog to Good Home", plain)
        self.assertNotIn("Condition:", plain)
        self.assertNotIn("condition (AI estimate)", html)
        self.assertIn("Price comparison not offered for pets", html)

    def test_email_uses_direct_mixed_lot_title_and_varied_condition(self):
        listing = public_listing("scan-lot", {
            "listing_id": "2673900996399023",
            "title": "Free stuff 3100 block Grindon Ave",
            "description": "Fan needs repair, shoe rack, scooter, dog bed.",
            "asking_price": 0,
            "vision": {"observations": {"listing_kind": "product", "item_type": "box"}},
            "condition_assessment": {"result": {"condition_grade": "needs_repair"}},
        })
        summary = alert_item_summary(listing, "free")
        rows = [{"scan_id": "scan-lot", "keyword": "free", "items": [summary]}]
        subject, plain = _summary_text(rows, "https://example.com/notify")
        html = _summary_html(rows, "https://example.com/notify")
        self.assertEqual(subject, "Scout: Fan, shoe rack, scooter and more — FREE")
        self.assertIn("Condition: varies by item", plain)
        self.assertIn("Assorted Items", html)
        self.assertNotIn("Needs Repair condition", html)
        self.assertIn("Why it matched: Facebook listed it as free", html)
        self.assertNotIn("3100 block", subject)

    def test_match_email_has_html_details_and_inline_saved_photo(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"SCOUT_PUBLIC_HOST": "scout.example.com"}, clear=True):
            db_path = Path(directory) / "alerts.sqlite"
            account = save_account({"email": "buyer@example.com", "zip_code": "21009",
                                    "radius_miles": 15, "terms": ["mower"],
                                    "plan": "free", "timezone": "America/New_York"}, db_path)
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE images (source TEXT, listing_id TEXT, "
                           "mime_type TEXT, image_data BLOB)")
                db.execute("INSERT INTO images VALUES (?, ?, ?, ?)",
                           ("facebook_marketplace", "123456789", "image/jpeg",
                            b"\xff\xd8\xff\xd9"))
            self.assertTrue(prepare_email_batch(account["id"], "photo-run", 1, db_path))
            scan = {"id": "scan-photo", "account_id": account["id"],
                    "email_batch_key": "photo-run", "status": "complete",
                    "keywords": ["mower"], "zip_code": "21009",
                    "radius_miles": 15, "listings": [{
                        "id": "123456789", "title": "Mower <special>",
                        "url": "https://www.facebook.com/marketplace/item/123456789/?ref=feed",
                        "item_type": "lawn_mower", "listing_kind": "product",
                        "condition_grade": "good", "asking_price": 25,
                        "location": "Edgewood, MD", "distance_miles_approx": 3.5,
                        "generated_description": "13-inch deck, battery included.",
                        "valuation": {"low": 50, "high": 110, "price_category": {
                            "label": "Good price", "comparison_count": 13}}}]}
            self.assertEqual(record_scan_matches(scan, db_path), 1)
            self.assertTrue(finish_email_scan(scan, db_path))
            with closing(sqlite3.connect(db_path)) as db:
                subject, plain, html, image_ids = db.execute(
                    "SELECT subject, body_text, body_html, image_ids_json "
                    "FROM email_outbox").fetchone()
            self.assertIn("Mower <special>", subject)
            self.assertIn("13-inch deck", plain)
            self.assertIn("$25.00", html)
            self.assertIn("Good price", html)
            self.assertIn("$50–$110", html)
            self.assertIn("Why it matched", html)
            self.assertIn("Open on Facebook", html)
            self.assertIn("?feedback=up&amp;listing=123456789#item-123456789", html)
            self.assertIn("?feedback=down&amp;listing=123456789#item-123456789", html)
            self.assertIn("Is Scout's analysis right?", plain)
            self.assertIn("https://www.facebook.com/marketplace/item/123456789/", plain)
            self.assertIn("cid:scout-123456789@scout.local", html)
            self.assertIn("Mower &lt;special&gt;", html)
            self.assertNotIn("<special>", html)
            self.assertEqual(json.loads(image_ids), ["123456789"])

            message = _build_message("scout@example.com", "buyer@example.com",
                                     subject, plain, html, json.loads(image_ids),
                                     db_path)
            self.assertIn("text/plain", [part.get_content_type()
                                          for part in message.walk()])
            self.assertIn("text/html", [part.get_content_type()
                                         for part in message.walk()])
            photos = [part for part in message.walk()
                      if part.get_content_type() == "image/jpeg"]
            self.assertEqual(len(photos), 1)
            self.assertEqual(photos[0]["Content-ID"],
                             "<scout-123456789@scout.local>")

    def test_alert_summary_flags_free_card_with_seller_asking_price(self):
        summary = alert_item_summary({
            "id": "123456789", "title": "Electric scooter",
            "asking_price": 180, "card_asking_price": 0,
            "price_source": "seller_description",
            "url": "https://www.facebook.com/marketplace/item/123456789/",
            "distance_miles_approx": 4,
        }, "electric scooter")
        self.assertEqual(summary["price"], 180)
        self.assertIn("Facebook card says FREE", summary["price_note"])
        self.assertIn("$180", summary["price_note"])
        titled = alert_item_summary({**{
            "id": "123456789", "title": "Chair asking $25",
            "asking_price": 25, "card_asking_price": 0,
            "price_source": "seller_title"}}, "chair")
        self.assertIn("seller title asks $25", titled["price_note"])
        self.assertEqual(summary["comparison_label"],
                         "Local price comparison unavailable")
        self.assertEqual(summary["facebook_url"],
                         "https://www.facebook.com/marketplace/item/123456789/")
        self.assertEqual(alert_item_summary({**summary,
            "url": "https://other.example/marketplace/item/123456789/"})["facebook_url"], "")

    def test_email_explains_why_a_price_comparison_is_pending(self):
        base = {"id": "123", "title": "Recliner", "asking_price": 0}
        shortage = alert_item_summary({**base, "valuation": {
            "status": "unvalued", "reason": "insufficient_local_furniture_comparables",
            "comparable_count": 1, "needed": 3}}, "free")
        self.assertEqual(shortage["comparison_label"],
                         "Price comparison pending: 1 of 3 compatible asking prices found")
        unknown = alert_item_summary({**base, "valuation": {
            "status": "unvalued", "reason": "price_group_unavailable"}}, "free")
        self.assertEqual(unknown["comparison_label"],
                         "No reliable comparison group for this item yet")
        parts = alert_item_summary({**base, "valuation": {
            "status": "unvalued", "reason": "missing_major_components"}}, "free")
        self.assertIn("major part is missing", parts["comparison_label"])

    def test_email_names_broader_reference_area(self):
        summary = alert_item_summary({
            "id": "123", "title": "Ryobi mower", "asking_price": 75,
            "valuation": {"low": 60, "high": 100,
                          "price_category": {"label": "Good price"},
                          "comparable_count": 3, "reference_scope_miles": 120,
                          "reference_scope_label": "regional"}}, "mower")
        self.assertIn("Regional asking reference", summary["comparison_label"])
        self.assertIn("within about 120 miles", summary["comparison_label"])
        self.assertIn("regional asking prices", summary["reason"])

    def test_bundle_email_names_separate_item_prices(self):
        summary = alert_item_summary({
            "id": "bundle", "title": "2 chairs and 1 couch", "asking_price": 120,
            "valuation": {"status": "provisional",
                          "price_type": "itemized_asking_range",
                          "low": 175, "high": 245, "comparable_count": 3,
                          "bundle_components": [{"label": "Chair", "quantity": 2,
                                                 "unit_low": 25, "unit_high": 35},
                                                {"label": "Sofa", "quantity": 1,
                                                 "unit_low": 125, "unit_high": 175}],
                          "price_category": {"label": "Best price"}}}, "furniture")
        self.assertIn("Combined individual asking reference", summary["comparison_label"])
        self.assertIn("single-item prices per component", summary["comparison_label"])
        self.assertIn("not whole-bundle listings", summary["reason"])
        self.assertEqual(summary["bundle_breakdown"][0]["quantity"], 2)

    def test_same_listing_is_emailed_to_each_matching_account(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"SCOUT_PUBLIC_HOST": "scout.example.com"}, clear=True):
            db = Path(directory) / "alerts.sqlite"
            recipients = []
            for email in ("first@example.com", "second@example.com"):
                account = save_account({"email": email, "zip_code": "21009",
                                        "radius_miles": 15, "terms": ["chair"],
                                        "plan": "free", "timezone": "America/New_York"}, db)
                batch = "batch-" + email.split("@")[0]
                self.assertTrue(prepare_email_batch(account["id"], batch, 1, db))
                scan = {"id": "scan-" + email.split("@")[0],
                        "account_id": account["id"], "email_batch_key": batch,
                        "status": "complete", "keywords": ["chair"],
                        "zip_code": "21009", "radius_miles": 15,
                        "listings": [{"id": "same-marketplace-id", "title": "Chair",
                                      "item_type": "chair", "listing_kind": "product",
                                      "condition_grade": "good", "asking_price": 0,
                                      "distance_miles_approx": 2}]}
                self.assertEqual(record_scan_matches(scan, db), 1)
                self.assertTrue(finish_email_scan(scan, db))
            self.assertEqual(deliver_pending_summaries(
                db, sender=lambda recipient, *_: recipients.append(recipient)), 2)
            self.assertEqual(set(recipients),
                             {"first@example.com", "second@example.com"})

    def test_repeat_listing_is_not_emailed_twice_to_same_account(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"SCOUT_PUBLIC_HOST": "scout.example.com"}, clear=True):
            db = Path(directory) / "alerts.sqlite"
            account = save_account({"email": "buyer@example.com", "zip_code": "21009",
                                    "radius_miles": 15, "terms": ["chair"],
                                    "plan": "plus_preview",
                                    "timezone": "America/New_York"}, db)
            scan = {"account_id": account["id"], "status": "complete",
                    "keywords": ["chair"], "zip_code": "21009",
                    "radius_miles": 15, "listings": [{
                        "id": "same-marketplace-id", "title": "Chair",
                        "item_type": "chair", "listing_kind": "product",
                        "condition_grade": "good", "asking_price": 0,
                        "distance_miles_approx": 2}]}
            self.assertTrue(prepare_email_batch(account["id"], "morning", 1, db))
            morning = {**scan, "id": "scan-morning", "email_batch_key": "morning"}
            self.assertEqual(record_scan_matches(morning, db), 1)
            self.assertTrue(finish_email_scan(morning, db))
            self.assertTrue(prepare_email_batch(account["id"], "noon", 1, db))
            noon = {**scan, "id": "scan-noon", "email_batch_key": "noon"}
            self.assertEqual(record_scan_matches(noon, db), 0)
            self.assertFalse(finish_email_scan(noon, db))
            self.assertEqual(email_status(db)["pending"], 1)

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
            self.assertIn("2 new finds", sent[0][1])
            self.assertIn("Chair, Desk", sent[0][1])
            self.assertIn("Chair", sent[0][2])
            self.assertIn("Desk", sent[0][2])
            self.assertIn("FREE", sent[0][2])
            self.assertIn("$20.00", sent[0][2])
            self.assertIn("https://scout.example.com/notify/scan/scan-chair#item-",
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
