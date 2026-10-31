"""Mower price cohorts must use evidence for power, size, and battery kit."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from marketplace_mower import mower_category, mower_peer, mower_variant
from marketplace_pricing import _with_saved_descriptions
from marketplace_value import estimate_value


class MowerCohortTests(unittest.TestCase):
    def test_battery_kit_is_separate_from_gas_and_tool_only(self):
        target = mower_variant(
            "RYOBI ONE+ 18V 13 in. Cordless Battery Walk Behind Push Lawn Mower",
            "Includes a 4.0 Ah lithium-ion battery and charger", "push_mower")
        gas = mower_variant("Bolens 20-inch Gas Push Lawn Mower", "", "push_mower")
        tool_only = mower_variant(
            "RYOBI ONE+ 18V 13 inch Cordless Push Lawn Mower",
            "No battery or charger", "push_mower")
        self.assertEqual(target, {"power_source": "battery", "brand": "ryobi",
                                  "deck_inches": 13, "voltage": 18,
                                  "kit": "battery_and_charger"})
        self.assertFalse(mower_peer(target, gas))
        self.assertFalse(mower_peer(target, tool_only))
        self.assertEqual(mower_category("push_mower", target)["key"],
                         "battery_push_mower")

    def test_starter_battery_does_not_turn_gas_mower_into_battery_mower(self):
        found = mower_variant("Gas push mower", "Electric start battery", "push_mower")
        self.assertEqual(found["power_source"], "gas")

    def test_seller_kit_and_no_gas_language_keep_battery_cohort(self):
        richmond = mower_variant(
            "Ryobi One+ 18V 13in Lawnmower (Battery Included)",
            "Ryobi battery powered lawnmower in great condition. "
            "Battery and charger included!", "push_mower")
        raleigh = mower_variant(
            'Ryobi 13" Push Mower',
            "RYOBI ONE+ 18V cordless electric push lawn mower with a 13-inch deck. "
            "No gas, oil, or pull cord needed. A larger gas mower isn't necessary. "
            "Battery and charger are included.", "push_mower")
        self.assertEqual(richmond["kit"], "battery_and_charger")
        self.assertEqual(raleigh["power_source"], "battery")
        self.assertEqual(raleigh["kit"], "battery_and_charger")

    def test_gas_engine_and_pull_start_claims_recover_saved_mower_types(self):
        descriptions = ("Starts on first or second pull", "Starts first yank",
                        "GCV160 engine with a 21-inch deck")
        for description in descriptions:
            with self.subTest(description=description):
                found = mower_variant("Self propelled mower", description,
                                      "self_propelled_mower")
                self.assertEqual(found["power_source"], "gas")

    def test_branded_gas_mower_does_not_borrow_competitor_prices(self):
        target = mower_variant("Toro 22 inch self propelled mower", "Gas engine",
                               "self_propelled_mower")
        snapper = mower_variant("Snapper 22 inch self propelled mower", "Gas engine",
                                "self_propelled_mower")
        toro = mower_variant("Toro 22 inch self propelled mower", "Gas engine",
                             "self_propelled_mower")
        self.assertFalse(mower_peer(target, snapper))
        self.assertTrue(mower_peer(target, toro))

    def test_electric_without_cord_or_battery_stays_unrated(self):
        target = {"listing_id": "target", "title": "Electric push mower",
                  "search_zip": "21009", "identity": {"id": None}}
        result = estimate_value(target, [])
        self.assertEqual(result["reason"], "mower_power_source_unclear")
        self.assertEqual(result["price_cohort"]["status"], "needs_evidence")

    def test_missing_power_pauses_price_rating(self):
        target = {"listing_id": "target", "title": "Push mower",
                  "search_zip": "21009", "identity": {"id": None}}
        comps = [self._comp(str(i), "Gas push mower", price)
                 for i, price in enumerate((40, 60, 80), 1)]
        result = estimate_value(target, comps)
        self.assertEqual(result["status"], "unvalued")
        self.assertEqual(result["reason"], "mower_power_source_unclear")
        self.assertNotIn("asking_context", result)

    def test_battery_mower_uses_only_matching_brand_voltage_deck_and_kit(self):
        target = {"listing_id": "target", "search_zip": "21009",
                  "title": "RYOBI ONE+ 18V 13 in. Cordless Battery Walk Behind Push Lawn Mower",
                  "description": "Includes battery and charger",
                  "identity": {"id": None}, "condition": {"grade": "unknown"}}
        comps = [self._comp("gas", "Gas push mower", 40),
                 self._comp("other_brand", "Greenworks 18V 13-inch cordless push mower", 30,
                            "Includes battery and charger"),
                 self._comp("tool_only", "RYOBI 18V 13-inch cordless push mower", 20,
                            "No battery or charger"),
                 self._comp("peer1", "RYOBI 18V 13-inch cordless push mower", 80,
                            "Includes battery and charger"),
                 self._comp("peer2", "RYOBI 18V 14-inch cordless push mower", 100,
                            "Includes battery and charger")]
        result = estimate_value(target, comps)
        self.assertEqual(result["status"], "unvalued")
        self.assertEqual(result["reason"], "insufficient_same_variant_comparables")
        self.assertEqual(result["comparable_count"], 2)
        self.assertEqual(result["price_cohort"]["key"], "battery_push_mower")
        comps.append(self._comp("peer3", "RYOBI 18V 13-inch cordless push mower", 120,
                                "Includes battery and charger"))
        result = estimate_value(target, comps)
        self.assertEqual(result["status"], "provisional")
        self.assertEqual(result["asking_price_sample"], [80.0, 100.0, 120.0])

    def test_saved_seller_text_is_available_to_comparable_matching(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "prices.sqlite"
            with closing(sqlite3.connect(db_path)) as db:
                db.execute("CREATE TABLE listings (source TEXT, listing_id TEXT, "
                           "description TEXT)")
                db.execute("INSERT INTO listings VALUES (?, ?, ?)",
                           ("facebook_marketplace", "1", "No battery or charger"))
                db.commit()
            rows = _with_saved_descriptions([{"source": "facebook_marketplace",
                                              "listing_id": "1", "title": "RYOBI 18V mower"}],
                                            db_path)
        self.assertEqual(rows[0]["description"], "No battery or charger")

    def test_generic_lawn_mower_label_can_match_exact_push_setup(self):
        target = {"listing_id": "target", "title": "RYOBI ONE+ 18V 13 in. "
                  "Cordless Push Lawn Mower", "description": "Battery and charger included",
                  "search_zip": "21009", "condition": {"grade": "unknown"}}
        refs = [self._comp(str(i), "RYOBI ONE+ 18V 13 in. cordless push mower",
                           price, "Battery and charger included")
                for i, price in enumerate((95, 120), 1)]
        generic = self._comp("generic", "Ryobi One+ 18V 13in Lawnmower",
                             155, "Battery and charger included")
        generic["reference_type"] = "lawn_mower"
        refs.append(generic)
        result = estimate_value(target, refs)
        self.assertEqual(result["status"], "provisional")
        generic["description"] += ". Self-propelled."
        result = estimate_value(target, refs)
        self.assertEqual(result["status"], "unvalued")

    @staticmethod
    def _comp(item_id, title, price, description=""):
        return {"source": "facebook_marketplace", "listing_id": item_id,
                "title": title, "description": description,
                "reference_type": "push_mower", "search_zip": "21009",
                "reference_distance_miles": 10, "asking_price": price,
                "condition": "good"}


if __name__ == "__main__":
    unittest.main()
