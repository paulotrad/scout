"""Votes are saved per person and listing, and email links only open review UI."""

from contextlib import closing
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import marketplace_web
from marketplace_feedback import (corrections_to_review, feedback_for_listings,
                                  feedback_metrics, save_feedback)


class FeedbackTests(unittest.TestCase):
    def test_feedback_metrics_group_current_votes_and_service_ads(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "feedback.sqlite"
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute("CREATE TABLE listings (source TEXT, listing_id TEXT, "
                                   "vision_json TEXT)")
                connection.executemany("INSERT INTO listings VALUES (?, ?, ?)", [
                    ("facebook_marketplace", "1", json.dumps({"observations": {
                        "category": "furniture"}})),
                    ("facebook_marketplace", "2", json.dumps({"observations": {
                        "category": "furniture"}})),
                ])
            save_feedback("alice", "scan-one", "1", "up", db_path=db)
            save_feedback("alice", "scan-one", "2", "down",
                          "service_or_ad", db)
            result = feedback_metrics(db)
            self.assertEqual(result["reviewed"], 2)
            self.assertEqual(result["unique_listings"], 2)
            self.assertFalse(result["sample_sufficient"])
            self.assertEqual(result["by_issue"],
                             [{"issue": "service_or_ad", "count": 1}])
            self.assertEqual(result["by_category"], [{
                "category": "furniture", "reviewed": 2,
                "confirmed": 1, "corrections": 1,
                "issues": {"service_or_ad": 1}}])
            save_feedback("alice", "scan-one", "2", "up", db_path=db)
            self.assertEqual(feedback_metrics(db)["corrections"], 0)

    def test_feedback_is_account_specific_and_revisable(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "feedback.sqlite"
            save_feedback("alice", "scan-one", "123", "down", "wrong_item", db)
            self.assertEqual(feedback_for_listings("alice", ["123"], db)["123"]["issue"],
                             "wrong_item")
            self.assertEqual(feedback_for_listings("bob", ["123"], db), {})
            save_feedback("alice", "scan-two", "123", "up", "wrong_item", db)
            current = feedback_for_listings("alice", ["123"], db)["123"]
            self.assertEqual(current["vote"], "up")
            self.assertEqual(current["issue"], "")
            with self.assertRaises(ValueError):
                save_feedback("alice", "scan-one", "123", "down", "untrusted", db)

    def test_correction_is_review_data_and_clears_when_vote_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "feedback.sqlite"
            saved = save_feedback("alice", "scan-one", "123", "down",
                                  "wrong_condition", db,
                                  correction={"corrected_condition": "fair",
                                              "note": "Wore one season"})
            self.assertEqual(saved["corrected_condition"], "fair")
            self.assertEqual(corrections_to_review(db_path=db)[0]["note"],
                             "Wore one season")
            with self.assertRaises(ValueError):
                save_feedback("alice", "scan-one", "123", "down", "wrong_price", db,
                              correction={"corrected_price": "nan"})
            save_feedback("alice", "scan-one", "123", "up", db_path=db)
            self.assertEqual(corrections_to_review(db_path=db), [])
            self.assertEqual(feedback_for_listings("alice", ["123"], db)["123"]
                             ["corrected_condition"], "")

    def test_public_feedback_requires_owner_and_real_scan_item(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scan_id = "scan-20261028-test"
            with (patch.object(marketplace_web, "ROOT", root),
                  patch.object(marketplace_web, "PUBLIC_HOST", "scout.example.com"),
                  patch.object(marketplace_web, "PUBLIC_WRITES", True),
                  patch.object(marketplace_web.Handler, "_identity",
                               lambda self: {"email": self.headers.get("X-Test-Email", ""),
                                             "subject": "verified"}),
                  patch.object(marketplace_web.Handler, "_owns_scan",
                               lambda self, sid, email: sid == scan_id and
                               email == "alice@example.com"),
                  patch.object(marketplace_web.Handler, "_account",
                               lambda self, email: {"id": "alice", "configured": True}),
                  patch.object(marketplace_web, "read_manifest",
                               return_value={"id": scan_id}),
                  patch.object(marketplace_web, "public_scan",
                               return_value={"listings": [{"id": "123"}]}),
                  patch.object(marketplace_web.Handler, "log_message")):
                server = ThreadingHTTPServer(("127.0.0.1", 0), marketplace_web.Handler)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    def request(method, path, email="alice@example.com", payload=None,
                                origin="https://scout.example.com"):
                        connection = HTTPConnection("127.0.0.1", server.server_port)
                        headers = {"Host": "scout.example.com", "X-Test-Email": email,
                                   "Origin": origin, "Content-Type": "application/json"}
                        body = json.dumps(payload).encode() if payload is not None else None
                        connection.request(method, path, body=body, headers=headers)
                        response = connection.getresponse()
                        result = response.status, json.loads(response.read())
                        connection.close()
                        return result

                    base = f"/notify/api/scans/{scan_id}"
                    self.assertEqual(request("POST", base + "/items/123/feedback",
                                             payload={"vote": "down",
                                                      "issue": "wrong_condition",
                                                      "correction": {
                                                          "corrected_condition": "fair"}})[0], 200)
                    status, result = request("GET", base + "/feedback")
                    self.assertEqual(status, 200)
                    self.assertEqual(result["feedback"]["123"]["issue"],
                                     "wrong_condition")
                    self.assertEqual(result["feedback"]["123"]["corrected_condition"],
                                     "fair")
                    self.assertEqual(request("POST", base + "/items/999/feedback",
                                             payload={"vote": "up"})[0], 404)
                    self.assertEqual(request("POST", base + "/items/123/feedback",
                                             email="bob@example.com",
                                             payload={"vote": "up"})[0], 404)
                    self.assertEqual(request("POST", base + "/items/123/feedback",
                                             payload={"vote": "up"},
                                             origin="https://elsewhere.example")[0], 403)
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
