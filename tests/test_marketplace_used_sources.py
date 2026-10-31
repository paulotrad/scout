"""Used-listing discovery must not become an unreviewed price quote."""

import unittest
from urllib.parse import parse_qs, urlsplit

from marketplace_used_sources import source_plan


class UsedSourcePlanTests(unittest.TestCase):
    def test_named_mower_uses_local_used_sources(self):
        plan = source_plan({"title": "RYOBI ONE+ 18V 13 in cordless push mower"})
        self.assertEqual(plan["category"], "home_improvement")
        self.assertEqual([source["name"] for source in plan["sources"]],
                         ["Craigslist", "OfferUp"])
        self.assertEqual(plan["price_status"], "unverified_used_search_leads")
        self.assertNotIn("price", plan)

    def test_shippable_electronics_add_mercari_and_encoded_query(self):
        plan = source_plan({"title": "Free Quest 3 $25",
                            "vision": {"observations": {
                                "category": "electronics", "listing_kind": "product"}}},
                           display_name="Quest 3 $25")
        self.assertEqual(plan["category"], "electronics")
        self.assertEqual(plan["query"], "Quest 3")
        self.assertEqual([source["name"] for source in plan["sources"]],
                         ["Craigslist", "OfferUp", "Mercari"])
        self.assertEqual(parse_qs(urlsplit(plan["sources"][1]["url"]).query)["q"],
                         ["Quest 3"])

    def test_bulky_appliance_and_furniture_stay_local(self):
        washer = source_plan({"title": "Whirlpool washer"})
        couch = source_plan({"title": "Leather couch"})
        self.assertEqual([row["name"] for row in washer["sources"]],
                         ["Craigslist", "OfferUp"])
        self.assertEqual([row["name"] for row in couch["sources"]],
                         ["Craigslist", "OfferUp"])

    def test_craigslist_browser_link_uses_scan_area(self):
        plan = source_plan({"title": "RYOBI mower"}, zip_code="21009",
                           radius_miles=45)
        query = parse_qs(urlsplit(plan["sources"][0]["url"]).query)
        self.assertEqual(query, {"query": ["RYOBI mower"],
                                 "postal": ["21009"],
                                 "search_distance": ["45"]})

    def test_sports_shoes_can_use_shippable_used_source(self):
        plan = source_plan({"title": "Men's Nike baseball cleats",
                            "vision": {"observations": {
                                "category": "sports_equipment", "listing_kind": "product"}}})
        self.assertEqual(plan["category"], "sports")
        self.assertEqual([row["name"] for row in plan["sources"]],
                         ["Craigslist", "OfferUp", "Mercari"])

    def test_bundle_pet_and_service_get_no_single_item_retail_leads(self):
        for listing in ({"title": "Mesa y 3 sillas"},
                        {"title": "Free puppy", "vision": {"observations": {
                            "category": "pets", "listing_kind": "product"}}},
                        {"title": "Lawn mower repair service",
                         "vision": {"observations": {"listing_kind": "service"}}},
                        {"title": "Free"}):
            with self.subTest(title=listing["title"]):
                self.assertIsNone(source_plan(listing))


if __name__ == "__main__":
    unittest.main()
