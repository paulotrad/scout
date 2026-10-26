"""The summary request preserves the original seller text for review."""

import json
from unittest.mock import Mock
import unittest

from marketplace_description_summary import polish_summary, summarize_description


class DescriptionSummaryTests(unittest.TestCase):
    def test_sends_seller_description_to_local_model_without_an_image(self):
        response = Mock()
        response.json.return_value = {"message": {"content": json.dumps({
            "snippets": ["Needs a new belt", "attachments missing", "Pick up ASAP"]
        })}}
        session = Mock()
        session.post.return_value = response
        summary = summarize_description(session,
                                        "Needs a new belt; attachments missing. Pick up ASAP.",
                                        "qwen3:8b", 30)
        payload = session.post.call_args.kwargs["json"]
        self.assertIn("Needs a new belt", payload["messages"][0]["content"])
        self.assertNotIn("images", payload["messages"][0])
        self.assertIn("new belt", summary)
        self.assertNotIn("Pick up", summary)

    def test_drops_overlong_snippet(self):
        response = Mock()
        response.json.return_value = {"message": {"content": json.dumps({
            "snippets": ["x" * 321]
        })}}
        session = Mock()
        session.post.return_value = response
        self.assertIsNone(summarize_description(session, "x" * 321, "qwen3:8b", 30))

    def test_drops_claim_not_in_seller_description(self):
        response = Mock()
        response.json.return_value = {"message": {"content": json.dumps({
            "snippets": ["Dark wood piano with bench.", "Item is not free."]
        })}}
        session = Mock()
        session.post.return_value = response
        summary = summarize_description(session, "Dark wood piano with bench.",
                                        "qwen3:8b", 30)
        self.assertEqual(summary, "Dark wood piano with bench.")

    def test_polish_removes_place_and_restores_working_claim(self):
        summary = polish_summary(
            "medium size; Ellicott City.",
            "Works well medium size, on the curbside at 8538 Autumn Rust Rd, Ellicott City")
        self.assertEqual(summary, "Works well medium size.")


if __name__ == "__main__":
    unittest.main()
