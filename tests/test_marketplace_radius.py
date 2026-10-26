"""Check the approximate local-radius filter without external geocoding calls."""

import unittest

from marketplace_radius import distance_miles, filter_result


class FakeGeocoder:
    places = {
        "21009": {"latitude": 39.46775, "longitude": -76.30122,
                  "display_name": "21009"},
        "Nearby, MD": {"latitude": 39.47, "longitude": -76.31,
                       "display_name": "Nearby, Maryland"},
        "Far, MD": {"latitude": 40.0, "longitude": -76.31,
                    "display_name": "Far, Maryland"},
    }

    def lookup(self, place):
        return self.places.get(place)


class MarketplaceRadiusTests(unittest.TestCase):
    def test_filter_separates_near_far_and_unknown_towns(self):
        data = {"requested_zip": "21009", "captured_at": "2026-10-22T00:00:00Z",
                "images_downloaded": 3, "database_images_stored": 3,
                "listings": [
                    {"listing_id": "1", "title": "Desk", "location": "Nearby, MD",
                     "image_file": "images/1.jpg"},
                    {"listing_id": "2", "title": "Piano", "location": "Far, MD",
                     "image_file": "images/2.jpg"},
                    {"listing_id": "3", "title": "Unknown", "location": None,
                     "image_file": "images/3.jpg"},
                ]}
        result = filter_result(data, FakeGeocoder(), 15)
        self.assertEqual([x["listing_id"] for x in result["listings"]], ["1"])
        self.assertEqual(result["excluded_count"], 1)
        self.assertEqual(result["unresolved_count"], 1)
        self.assertEqual(result["images_downloaded"], 1)
        self.assertEqual(result["database_images_stored_in_source_run"], 3)
        self.assertEqual(result["radius_filter_method"],
                         "approximate_seller_town_centroid_distance")
        self.assertEqual(distance_miles((39.46775, -76.30122),
                                        (39.46775, -76.30122)), 0)


if __name__ == "__main__":
    unittest.main()
