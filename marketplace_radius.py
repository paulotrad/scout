"""Approximate a ZIP-radius filter from Marketplace's displayed seller towns.

Facebook's Free Stuff page can show distant cards even when its sidebar shows
a smaller radius. This checks town centroids and labels the result approximate;
it cannot confirm a seller's precise address or guarantee complete coverage.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import time

import requests


NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
ZIP_LOOKUP = "https://api.zippopotam.us/us/"
ATTRIBUTION = ("ZIP centers and postal towns: GeoNames via Zippopotam.us "
               "when available; seller towns: © OpenStreetMap contributors "
               "(Nominatim)")
USER_AGENT = "MarketplaceResearcher/0.1 (single-user local Marketplace project)"
STATES = dict(zip(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split(),
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia".split("|")
))


def distance_miles(first: tuple[float, float], second: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, first)
    lat2, lon2 = map(math.radians, second)
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    a = (math.sin(delta_lat / 2) ** 2 +
         math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2)
    return 3958.7613 * 2 * math.asin(min(1, math.sqrt(a)))


class TownGeocoder:
    """Cached ZIP and seller-town lookup for a small local run."""

    def __init__(self, cache_file: Path):
        self.cache_file = cache_file
        self.cache = (json.loads(cache_file.read_text(encoding="utf-8"))
                      if cache_file.is_file() else {})
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies.clear()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def close(self) -> None:
        self.session.close()

    def zip_info(self, zip_code: str) -> dict | None:
        """Get the ZIP's postal town and center before falling back to OSM.

        A generic OSM postal-code result can name a nearby large city instead
        of the actual postal town, causing Marketplace to scan the wrong area.
        """
        if not re.fullmatch(r"\d{5}", zip_code):
            return None
        cache_key = f"zip-info:{zip_code}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        try:
            response = self.session.get(ZIP_LOOKUP + zip_code, timeout=15)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            return None
        info = None
        if (isinstance(payload, dict) and
                payload.get("post code") == zip_code and
                payload.get("country abbreviation") == "US"):
            for place in payload.get("places") or []:
                try:
                    state = place["state abbreviation"]
                    locality = place["place name"].strip()
                    latitude = float(place["latitude"])
                    longitude = float(place["longitude"])
                    if (state in STATES and locality and
                            -90 <= latitude <= 90 and -180 <= longitude <= 180):
                        info = {"locality": locality, "state": state,
                                "latitude": latitude, "longitude": longitude}
                        break
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
        self.cache[cache_key] = info
        self._save()
        return info

    def lookup(self, place: str) -> dict | None:
        if re.fullmatch(r"\d{5}", place):
            info = self.zip_info(place)
            if info:
                chosen = {"latitude": info["latitude"],
                          "longitude": info["longitude"],
                          "display_name": (f"{place}, {info['locality']}, "
                                           f"{STATES[info['state']]}, United States")}
                if self.cache.get(place) != chosen:
                    self.cache[place] = chosen
                    self._save()
                return chosen
        if place in self.cache:
            return self.cache[place]
        if re.fullmatch(r"\d{5}", place):
            parameters = {"postalcode": place, "countrycodes": "us",
                          "format": "jsonv2", "addressdetails": 1, "limit": 5}
            city = state = None
        else:
            match = re.fullmatch(r"(.+),\s*([A-Z]{2})", place)
            if not match or match.group(2) not in STATES:
                self.cache[place] = None
                self._save()
                return None
            city, state = match.groups()
            parameters = {"q": f"{city}, {STATES[state]}, United States",
                          "countrycodes": "us", "format": "jsonv2",
                          "addressdetails": 1, "limit": 5}
        remaining = 1.1 - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)
        response = self.session.get(NOMINATIM, params=parameters, timeout=20)
        self.last_request = time.monotonic()
        response.raise_for_status()
        options = response.json()
        chosen = None
        for option in options:
            address = option.get("address") or {}
            display = option.get("display_name", "")
            if state and (address.get("state") != STATES[state] or
                          not display.casefold().startswith(city.casefold())):
                continue
            chosen = {"latitude": float(option["lat"]),
                      "longitude": float(option["lon"]),
                      "display_name": display}
            break
        self.cache[place] = chosen
        self._save()
        return chosen

    def locality_for_zip(self, zip_code: str, location: dict, state: str) -> str | None:
        """Resolve a ZIP centroid to a nearby named place for site pickers.

        Postal-code search results often contain only county and state, while
        Marketplace's picker offers a town name. Cache the reverse result and
        require the same ZIP and state before using that town as a search query.
        """
        cache_key = f"zip-locality:{zip_code}"
        if isinstance(self.cache.get(cache_key), str) and self.cache[cache_key]:
            return self.cache[cache_key]
        if not re.fullmatch(r"\d{5}", zip_code) or state not in STATES:
            return None
        remaining = 1.1 - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)
        response = self.session.get(NOMINATIM_REVERSE, params={
            "lat": location["latitude"], "lon": location["longitude"],
            "format": "jsonv2", "addressdetails": 1, "zoom": 14,
        }, timeout=20)
        self.last_request = time.monotonic()
        response.raise_for_status()
        address = response.json().get("address") or {}
        locality = None
        if (address.get("country_code") == "us" and
                address.get("state") == STATES[state] and
                address.get("postcode") == zip_code):
            locality = next((address[field].strip() for field in
                             ("city", "town", "village", "hamlet", "suburb",
                              "municipality")
                             if isinstance(address.get(field), str) and
                             address[field].strip()), None)
        self.cache[cache_key] = locality
        self._save()
        return locality

    def _save(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps(self.cache, indent=2,
                                               ensure_ascii=False), encoding="utf-8")


def filter_result(data: dict, geocoder: TownGeocoder, radius_miles: int) -> dict:
    center_zip = data.get("requested_zip")
    if not isinstance(center_zip, str) or not re.fullmatch(r"\d{5}", center_zip):
        raise ValueError("Run has no five-digit requested ZIP")
    center = geocoder.lookup(center_zip)
    if center is None:
        raise ValueError("Could not locate the search ZIP")
    center_point = (center["latitude"], center["longitude"])
    included, excluded, unresolved = [], [], []
    for original in data.get("listings", []):
        listing = dict(original)
        place = listing.get("location")
        coords = geocoder.lookup(place) if isinstance(place, str) else None
        if coords is None:
            listing["radius_check"] = {"status": "location_unresolved"}
            unresolved.append(listing)
            continue
        miles = round(distance_miles(center_point,
                                     (coords["latitude"], coords["longitude"])), 2)
        listing["radius_check"] = {
            "status": "included" if miles <= radius_miles else "excluded",
            "distance_miles_approx": miles,
            "basis": "seller town centroid; exact listing location is unknown",
            "geocoded_place": coords["display_name"],
            "near_boundary": abs(miles - radius_miles) <= 3,
        }
        (included if miles <= radius_miles else excluded).append(listing)
    result = dict(data)
    result["source_run_captured_at"] = data.get("captured_at")
    result["radius_filtered_at"] = datetime.now(timezone.utc).isoformat()
    result["radius_miles_target"] = radius_miles
    result["radius_filter_method"] = "approximate_seller_town_centroid_distance"
    result["radius_filter_limitations"] = [
        "Facebook's Free Stuff feed may include cards beyond the selected UI radius",
        "seller towns are approximate and do not reveal exact listing distance",
        "only cards exposed by Facebook during this browser session can be evaluated",
    ]
    result["geocoding_attribution"] = ATTRIBUTION
    result["geocoding_policy"] = "https://operations.osmfoundation.org/policies/nominatim/"
    result["listings"] = included
    result["excluded_listings"] = excluded
    result["unresolved_listings"] = unresolved
    result["listing_count"] = len(included)
    result["excluded_count"] = len(excluded)
    result["unresolved_count"] = len(unresolved)
    result["identified_count"] = sum(bool(row.get("identity", {}).get("id"))
                                     for row in included)
    result["valued_count"] = sum(row.get("valuation", {}).get("status") in
                                 {"estimated", "provisional"} for row in included)
    result["images_downloaded_in_source_run"] = data.get("images_downloaded")
    result["images_downloaded"] = sum(bool(row.get("image_file")) for row in included)
    if "database_images_stored" in result:
        result["database_images_stored_in_source_run"] = result.pop("database_images_stored")
    result["database_images_selected"] = result["images_downloaded"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Approximate a Marketplace ZIP-radius filter")
    parser.add_argument("input", type=Path)
    parser.add_argument("--radius-miles", type=int, required=True)
    parser.add_argument("--cache", type=Path, default=Path("runs/town_geocodes.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.radius_miles < 1:
        parser.error("Radius must be positive")
    data = json.loads(args.input.read_text(encoding="utf-8"))
    geocoder = TownGeocoder(args.cache)
    try:
        result = filter_result(data, geocoder, args.radius_miles)
    finally:
        geocoder.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    print(json.dumps({"included": result["listing_count"],
                      "excluded": result["excluded_count"],
                      "unresolved": result["unresolved_count"],
                      "output": str(args.output.resolve())}, indent=2))


if __name__ == "__main__":
    main()
