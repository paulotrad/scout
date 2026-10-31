"""Check the approximate local-radius filter without external geocoding calls."""

import unittest
from pathlib import Path
import tempfile
from unittest.mock import Mock

from marketplace_radius import STATES, TownGeocoder, distance_miles, filter_result


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
    def test_zip_lookup_replaces_cached_large_city_with_postal_town(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "geocodes.json"
            cache.write_text('{"90210": {"latitude": 34.09388, '
                             '"longitude": -118.41128, '
                             '"display_name": "90210, Los Angeles, California, '
                             'United States"}}', encoding="utf-8")
            geocoder = TownGeocoder(cache)
            response = Mock()
            response.json.return_value = {
                "post code": "90210", "country abbreviation": "US",
                "places": [{"place name": "Beverly Hills",
                            "state abbreviation": "CA", "latitude": "34.0901",
                            "longitude": "-118.4065"}]}
            geocoder.session.get = Mock(return_value=response)
            location = geocoder.lookup("90210")
            self.assertEqual(location["display_name"],
                             "90210, Beverly Hills, California, United States")
            self.assertEqual(location["latitude"], 34.0901)
            self.assertEqual(geocoder.lookup("90210"), location)
            geocoder.session.get.assert_called_once()
            geocoder.close()

    def test_zip_lookup_rejects_mismatched_postal_response(self):
        with tempfile.TemporaryDirectory() as directory:
            geocoder = TownGeocoder(Path(directory) / "geocodes.json")
            response = Mock()
            response.json.return_value = {
                "post code": "90211", "country abbreviation": "US",
                "places": [{"place name": "Beverly Hills",
                            "state abbreviation": "CA", "latitude": "34.0901",
                            "longitude": "-118.4065"}]}
            geocoder.session.get = Mock(return_value=response)
            self.assertIsNone(geocoder.zip_info("90210"))
            geocoder.close()

    def test_zip_locality_accepts_matching_zip_and_caches_result(self):
        with tempfile.TemporaryDirectory() as directory:
            geocoder = TownGeocoder(Path(directory) / "geocodes.json")
            response = Mock()
            response.json.return_value = {"address": {
                "village": "Forest Hill", "state": "Maryland",
                "postcode": "21050", "country_code": "us"}}
            geocoder.session.get = Mock(return_value=response)
            location = {"latitude": 39.5787648, "longitude": -76.3768447}
            self.assertEqual(geocoder.locality_for_zip("21050", location, "MD"),
                             "Forest Hill")
            self.assertEqual(geocoder.locality_for_zip("21050", location, "MD"),
                             "Forest Hill")
            geocoder.session.get.assert_called_once()
            geocoder.close()

    def test_zip_locality_rejects_other_postcode(self):
        with tempfile.TemporaryDirectory() as directory:
            geocoder = TownGeocoder(Path(directory) / "geocodes.json")
            response = Mock()
            response.json.return_value = {"address": {
                "town": "Wrong Town", "state": "Maryland",
                "postcode": "21009", "country_code": "us"}}
            geocoder.session.get = Mock(return_value=response)
            self.assertIsNone(geocoder.locality_for_zip("21050", {
                "latitude": 39.5787648, "longitude": -76.3768447}, "MD"))
            geocoder.close()

    def test_zip_locality_can_use_suburb_when_no_town_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            geocoder = TownGeocoder(Path(directory) / "geocodes.json")
            response = Mock()
            response.json.return_value = {"address": {
                "suburb": "Abingdon", "state": "Maryland",
                "postcode": "21009", "country_code": "us"}}
            geocoder.session.get = Mock(return_value=response)
            self.assertEqual(geocoder.locality_for_zip("21009", {
                "latitude": 39.4677499, "longitude": -76.3012224}, "MD"),
                "Abingdon")
            geocoder.close()

    def test_town_geocoder_accepts_all_us_states_and_dc(self):
        self.assertEqual(len(STATES), 51)
        self.assertEqual(STATES["NY"], "New York")
        self.assertEqual(STATES["CA"], "California")

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
