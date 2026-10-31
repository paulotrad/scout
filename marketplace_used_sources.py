"""Used-listing research leads for sparse Facebook price comparisons.

Links are discovery aids. A result only becomes a comparable after item,
condition, location, and seller ask are checked.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urlencode

from marketplace_bundle import extract_bundle
from marketplace_listing_copy import mixed_item_lot
from marketplace_vision import live_animal_listing, seller_offer_override
from product_candidate import suggest_product_candidate


SHIPPABLE_SOURCES = {"electronics", "sports", "general"}


def _vertical(category: str) -> str:
    category = category.casefold().replace(" ", "_")
    if category in {"garden_equipment", "tools", "home_improvement", "hardware",
                    "building_materials", "outdoor_equipment"}:
        return "home_improvement"
    if category in {"appliance", "appliances", "kitchen_appliances",
                    "household_appliances"}:
        return "appliances"
    if category in {"electronics", "computer", "computers", "gaming",
                    "phones", "audio_equipment"}:
        return "electronics"
    if category in {"personal_transport", "mobility_equipment"}:
        return "transport"
    if category in {"sports_equipment", "sporting_goods", "footwear", "apparel"}:
        return "sports"
    if category == "furniture":
        return "furniture"
    return "general"


def _craigslist_url(query: str, zip_code: str, radius_miles: int | float | None) -> str:
    params = {"query": query}
    if re.fullmatch(r"\d{5}", str(zip_code or "")):
        params["postal"] = str(zip_code)
        if isinstance(radius_miles, (int, float)) and 1 <= radius_miles <= 1000:
            params["search_distance"] = round(radius_miles)
    return "https://www.craigslist.org/search/sss?" + urlencode(params)


def source_plan(listing: dict, *, display_name: str = "", zip_code: str = "",
                radius_miles: int | float | None = None) -> dict | None:
    """Suggest where to inspect used asks for one physical product.

    The plan contains no price. Search hits are not approved comparables.
    """
    title = str(listing.get("title") or "")
    description = str(listing.get("description") or "")
    observations = (listing.get("vision") or {}).get("observations") or {}
    if (seller_offer_override(title, description) or
            live_animal_listing(title, description) or
            str(observations.get("category") or "").casefold() in {"pet", "pets"} or
            extract_bundle(listing) or
            mixed_item_lot(title, description) or
            observations.get("listing_kind") not in {None, "", "product"}):
        return None
    candidate = suggest_product_candidate(title)
    types = candidate.get("types") or []
    category = (types[0]["category"] if len(types) == 1 else
                observations.get("category") or "")
    name = display_name or title
    name = re.sub(r"\b(?:free|for sale|pickup only)\b", " ", name, flags=re.I)
    name = re.sub(r"\$\s*\d[\d,.]*", " ", name)
    name = " ".join(name.split())[:110].strip(" -.,!?")
    if not name or name.casefold() in {"item", "items", "stuff", "misc"}:
        return None
    vertical = _vertical(category)
    sources = [{"name": "Craigslist", "scope": "local",
                "url": _craigslist_url(name, zip_code, radius_miles)},
               {"name": "OfferUp", "scope": "set_location_on_site",
                "url": f"https://offerup.com/search?q={quote_plus(name)}"}]
    if vertical in SHIPPABLE_SOURCES:
        sources.append({"name": "Mercari", "scope": "online_used",
                        "url": f"https://www.mercari.com/search/?keyword={quote_plus(name)}"})
    return {
        "category": vertical,
        "query": name,
        "sources": sources,
        "price_status": "unverified_used_search_leads",
    }
