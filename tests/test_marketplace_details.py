"""Verify seller text stops before approximate location and profile details."""

import unittest

from marketplace_details import extract_description


class MarketplaceDetailTests(unittest.TestCase):
    def test_extracts_description_and_condition_from_visible_details(self):
        body = ("Marketplace\nSolid wood desk\n$0\nMessage\nSave\nDetails\n"
                "Condition\nUsed - Good\nSolid wood desk. Has some marks.\n"
                "Pick up in Dublin.\nStreet, MD · Location is approximate\n"
                "Seller information\nName")
        result = extract_description(body)
        self.assertEqual(result["description"],
                         "Solid wood desk. Has some marks.\nPick up in Dublin.")
        self.assertEqual(result["seller_condition_label"], "Used - Good")
        self.assertEqual(result["description_status"], "visible")
        self.assertNotIn("Name", result["description"])

    def test_marks_missing_detail_without_guessing(self):
        result = extract_description("Log In\nSee more on Facebook")
        self.assertEqual(result["description_status"], "details_not_visible")
        self.assertIsNone(result["description"])


if __name__ == "__main__":
    unittest.main()
