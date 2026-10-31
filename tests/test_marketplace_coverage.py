"""Price coverage counts track supported mower groups without inflating duplicates."""

from datetime import datetime, timedelta, timezone
import unittest

from marketplace_coverage import mower_coverage


class MowerCoverageTests(unittest.TestCase):
    def test_recent_approved_categories_are_distinct_and_ambiguous_rows_separate(self):
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        recent = (now - timedelta(days=2)).isoformat()
        old = (now - timedelta(days=366)).isoformat()
        def row(item_id, title, observed=recent, approved=True):
            return {"source": "facebook_marketplace", "listing_id": item_id,
                    "reference_type": "push_mower", "title": title,
                    "description": "Includes battery and charger", "observed_at": observed,
                    "price_reviewed": approved}
        rows = [row("1", "RYOBI 18V 13-inch cordless push mower"),
                row("1", "RYOBI 18V 13-inch cordless push mower"),
                row("2", "Push mower"), row("3", "Gas push mower", old),
                row("4", "Gas push mower", approved=False)]
        result = mower_coverage(rows, now)
        counts = {group["key"]: group["count"] for group in result["categories"]}
        self.assertEqual(counts["battery_push_mower"], 1)
        self.assertEqual(counts["gas_push_mower"], 0)
        self.assertEqual(result["needs_category_review"], 1)
        self.assertEqual(result["total_walk_behind"], 2)


if __name__ == "__main__":
    unittest.main()
