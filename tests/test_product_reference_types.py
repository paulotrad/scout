"""Comparable prices must keep materially different product forms separate."""

import unittest

from product_candidate import suggest_product_candidate


class ProductReferenceTypesTests(unittest.TestCase):
    def test_specific_mower_types_do_not_collapse_to_lawn_mower(self):
        cases = {
            "Riding lawn mower": "riding_mower",
            "Push lawn mower": "push_mower",
            "Zero-turn lawn mower": "zero_turn_mower",
            "Zero turn riding lawnmower": "zero_turn_mower",
            "Robot lawn mower": "robot_mower",
            "Manual Reel Mower": "reel_mower",
            "Self Propelled Recycler Lawn Mower": "self_propelled_mower",
            "Troy-Bilt walk-behind lawn mower self propelled": "self_propelled_mower",
            "Husqvarna law mower self - Propelled": "self_propelled_mower",
            "Commercial lawn mower": "commercial_walk_behind_mower",
            "Law mower for sale": "lawn_mower",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                result = suggest_product_candidate(title)
                self.assertEqual([row["type"] for row in result["types"]], [expected])

    def test_furniture_forms_are_distinct(self):
        for title, expected in (
            ("Dining table", "dining_table"),
            ("Coffee table", "coffee_table"),
            ("Office chair", "office_chair"),
            ("Rocking chair", "rocking_chair"),
            ("Recliner chair", "recliner_chair"),
            ("China cabinet", "china_cabinet"),
        ):
            with self.subTest(title=title):
                self.assertEqual([row["type"] for row in suggest_product_candidate(title)["types"]],
                                 [expected])

    def test_appliance_and_scooter_forms(self):
        for title, expected in (
            ("Refrigerator", "refrigerator"),
            ("Washing machine", "washer"),
            ("Washer dryer combo", "washer_dryer_combo"),
            ("Electric scooter", "electric_scooter"),
            ("Mobility scooter", "mobility_scooter"),
        ):
            with self.subTest(title=title):
                self.assertEqual([row["type"] for row in suggest_product_candidate(title)["types"]],
                                 [expected])

    def test_bundle_remains_ambiguous(self):
        self.assertTrue(suggest_product_candidate("Dining table and chairs")["needs_review"])
        self.assertTrue(suggest_product_candidate(
            "Laura Ashley dresser and nightstand")["needs_review"])


if __name__ == "__main__":
    unittest.main()
