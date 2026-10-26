"""Text-first product identity and transparent secondhand asking-value estimates.

Values are hypotheses from comparable *asking* prices, not verified sale prices.
No model is trained until labeled item identities and enough comparable listings exist.
"""

from __future__ import annotations

from statistics import median
import math
import re
from typing import Any

from product_candidate import suggest_product_candidate


CONDITION_FACTORS = {
    "new": 1.00,
    "like_new": 0.88,
    "good": 0.72,
    "fair": 0.52,
    "parts": 0.22,
}


def _words(value: str) -> set[str]:
    separated = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", value.casefold())
    return set(re.findall(r"[a-z0-9]+", separated))


def infer_condition(text: str) -> dict[str, Any]:
    """Infer condition only when the seller's text actually gives a clue."""
    value = text.casefold()
    patterns = (
        ("parts", r"\b(for parts|not working|broken|doesn.?t work|needs repair)\b"),
        ("like_new", r"\b(like new|almost new|mint condition|barely used)\b"),
        ("new", r"\b(brand new|new in box|sealed|never used|unopened)\b"),
        ("fair", r"\b(fair condition|as is|well used|worn|damage[ds]?)\b"),
        ("good", r"\b(good condition|works great|fully working|excellent condition)\b"),
    )
    for grade, pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return {"grade": grade, "basis": "seller_text", "evidence": match.group(0)}
    return {"grade": "unknown", "basis": "no_condition_claim", "evidence": None}


def _catalog_match(title: str, catalog: list[dict[str, Any]]) -> dict[str, Any] | None:
    query = _words(title)
    ranked = []
    for item in catalog:
        if not item.get("id") or not item.get("name"):
            continue
        aliases = [item["name"], *(item.get("aliases") or [])]
        for alias in aliases:
            tokens = _words(alias)
            if not tokens or not tokens <= query:
                continue
            required = _words(" ".join(item.get("must_contain") or []))
            if required and not required <= query:
                continue
            # Distinctive, longer aliases beat broad family names.
            score = min(0.99, 0.65 + 0.06 * len(tokens))
            ranked.append((score, len(tokens), item))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    winner = ranked[0][2]
    if any(row[2]["id"] != winner["id"] and row[0] == ranked[0][0]
           for row in ranked[1:]):
        return None
    return {"id": winner["id"], "name": winner["name"],
            "category": winner.get("category", "other"),
            "confidence": ranked[0][0], "method": "catalog_alias",
            "reference_url": winner.get("reference_url")}


def identify_item(title: str, catalog: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return a canonical identity only when text includes distinguishing details."""
    title = title.strip()
    if not title:
        return {"id": None, "name": None, "category": None,
                "confidence": 0.0, "method": "unresolved"}
    match = _catalog_match(title, catalog or [])
    if match:
        return match

    gpu = re.search(r"\b(?:geforce\s*)?rtx\s*(\d{4})\s*(ti|super)?\s*(\d{1,2})\s*gb\b",
                    title, re.I)
    if gpu:
        generation, variant, memory = gpu.groups()
        variant = (variant or "").lower()
        suffix = f"-{variant}" if variant else ""
        name = f"NVIDIA GeForce RTX {generation}{' ' + variant.title() if variant else ''} {memory}GB"
        return {"id": f"gpu:nvidia-rtx-{generation}{suffix}-{memory}gb",
                "name": name, "category": "graphics_card", "confidence": 0.85,
                "method": "model_pattern"}

    quest = re.search(r"\b(?:meta|oculus)?\s*quest\s*(2|3s|3|pro)\b", title, re.I)
    if quest:
        model = quest.group(1).upper()
        return {"id": f"headset:meta-quest-{model.lower()}",
                "name": f"Meta Quest {model}", "category": "vr_headset",
                "confidence": 0.82, "method": "model_pattern"}

    vehicle = re.search(
        r"\b(19\d{2}|20\d{2})\s+(bmw|jeep|chevrolet|chevy|ducati)\b[\s-]+(.+)",
        title, re.I,
    )
    if vehicle:
        year, make, rest = vehicle.groups()
        make = "Chevrolet" if make.casefold() == "chevy" else make.title()
        model = re.sub(r"\b(sedan|coupe|2d|4d|miles|mi|clean title|for sale)\b.*$", "", rest,
                       flags=re.I).strip(" -.,")
        model = " ".join(model.split()[:5])
        if model:
            slug = re.sub(r"[^a-z0-9]+", "-", f"{year}-{make}-{model}".casefold()).strip("-")
            return {"id": f"vehicle:{slug}", "name": f"{year} {make} {model}",
                    "category": "vehicle", "confidence": 0.67,
                    "method": "title_pattern_needs_trim_verification"}

    return {"id": None, "name": None, "category": None,
            "confidence": 0.0, "method": "unresolved"}


def _money(value: Any) -> float | None:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) and 0 < amount < 10_000_000 else None


def furniture_signature(title: str) -> dict[str, Any] | None:
    """Identify a broad furniture form and only explicitly named materials."""
    candidate = suggest_product_candidate(title)
    furniture = [row["type"] for row in candidate["types"]
                 if row["category"] == "furniture"]
    if candidate["needs_review"] or len(furniture) != 1:
        return None
    return {"product_type": furniture[0],
            "material_clues": candidate["material_clues"]}


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    before = int(position)
    after = min(before + 1, len(ordered) - 1)
    return ordered[before] + (ordered[after] - ordered[before]) * (position - before)


def _furniture_asking_range(listing: dict[str, Any], comps: list[dict[str, Any]],
                            min_comps: int) -> dict[str, Any]:
    """Use local, same-form listings as a cautious asking-price reference."""
    signature = furniture_signature(listing.get("title", ""))
    zip_code = listing.get("search_zip")
    if not signature or not zip_code:
        return {"status": "unvalued", "reason": "furniture_type_or_search_zip_unconfirmed"}
    target_materials = set(signature["material_clues"])
    target_grade = (listing.get("condition") or {}).get("grade", "unknown")
    matched = []
    seen = set()
    for comp in comps:
        source = comp.get("source")
        comp_id = comp.get("listing_id")
        if source not in {"facebook_marketplace", "craigslist"} or not comp_id:
            continue
        key = (source, str(comp_id))
        if key in seen or (source == "facebook_marketplace" and
                           str(comp_id) == str(listing.get("listing_id"))):
            continue
        if str(comp.get("search_zip")) != str(zip_code):
            continue
        other = furniture_signature(comp.get("title", ""))
        if not other or other["product_type"] != signature["product_type"]:
            continue
        if target_materials and not target_materials <= set(other["material_clues"]):
            continue
        comp_grade = comp.get("condition") or "unknown"
        if comp_grade == "unknown":
            comp_grade = infer_condition(comp.get("title", ""))["grade"]
        if target_grade != "unknown" and comp_grade not in {"unknown", target_grade}:
            continue
        if target_grade == "unknown" and comp_grade in {"new", "parts"}:
            continue
        price = _money(comp.get("asking_price"))
        if price is None:
            continue
        seen.add(key)
        matched.append((price, comp))
    if len(matched) < min_comps:
        return {"status": "unvalued", "reason": "insufficient_local_furniture_comparables",
                "comparable_count": len(matched), "needed": min_comps}
    prices = [price for price, _ in matched]
    limitations = ["asking prices are not completed sale prices",
                   "pickup effort, dimensions, stains, and hidden damage may change value"]
    if any(comp["source"] == "craigslist" for _, comp in matched):
        limitations.append("Craigslist comparables were supplied by the user")
    return {
        "status": "provisional", "currency": "USD", "price_type": "local_asking_range",
        "low": round(_percentile(prices, 0.25), 2),
        "high": round(_percentile(prices, 0.75), 2),
        "midpoint": round(median(prices), 2),
        "basis": "middle half of local asking prices for the same furniture form",
        "product_type": signature["product_type"],
        "material_clues_required": sorted(target_materials),
        "search_zip": str(zip_code),
        "comparable_count": len(matched),
        "sources": sorted({comp["source"] for _, comp in matched}),
        "comparable_urls": [comp.get("source_url") for _, comp in matched
                            if comp.get("source_url")],
        "condition_assumption": target_grade,
        "confidence": "low",
        "limitations": limitations,
    }


def estimate_value(listing: dict[str, Any], comps: list[dict[str, Any]],
                   catalog_item: dict[str, Any] | None = None,
                   min_comps: int = 3) -> dict[str, Any]:
    """Estimate a broad range from other listings for the exact same item ID.

    Comparables are asking prices. Condition adjustments and a 15% negotiation
    discount are explicit assumptions; the output is not a sale-price prediction.
    """
    identity = listing.get("identity") or {}
    item_id = identity.get("id")
    if identity.get("category") == "vehicle":
        return {"status": "unvalued", "reason": "vehicle_needs_mileage_title_and_history"}
    if not item_id or identity.get("confidence", 0) < 0.75:
        if furniture_signature(listing.get("title", "")):
            return _furniture_asking_range(listing, comps, min_comps)
        return {"status": "unvalued", "reason": "item_identity_unconfirmed"}

    comparable = []
    unknown_comp_condition = 0
    seen_ids = set()
    for comp in comps:
        comp_id = comp.get("listing_id")
        source_key = (comp.get("source") or "unknown", str(comp_id))
        if not comp_id or source_key in seen_ids:
            continue
        if (comp.get("item_id") != item_id or
                (listing.get("listing_id") and comp_id == listing["listing_id"] and
                 comp.get("source") in {None, "facebook_marketplace"})):
            continue
        price = _money(comp.get("asking_price"))
        if price is None:
            continue
        seen_ids.add(source_key)
        grade = comp.get("condition", "unknown")
        factor = CONDITION_FACTORS.get(grade)
        if factor is None:
            # Search cards often omit condition. This is explicitly a rough
            # assumption, so the resulting estimate stays low confidence.
            factor = CONDITION_FACTORS["good"]
            unknown_comp_condition += 1
        comparable.append((price / factor, comp))
    if len(comparable) < min_comps:
        if identity.get("category") == "furniture":
            return _furniture_asking_range(listing, comps, min_comps)
        retail = _money((catalog_item or {}).get("new_price_usd"))
        if retail and identity.get("category") not in {"vehicle", "food"}:
            grade = (listing.get("condition") or {}).get("grade", "unknown")
            if grade == "unknown":
                low_factor = CONDITION_FACTORS["fair"]
                high_factor = CONDITION_FACTORS["like_new"]
            else:
                low_factor = high_factor = CONDITION_FACTORS[grade]
            return {
                "status": "provisional", "currency": "USD",
                "low": round(retail * low_factor * 0.60, 2),
                "high": round(retail * high_factor * 0.95, 2),
                "basis": "supplied new retail reference times assumed resale and condition factors",
                "retail_reference_usd": retail, "comparable_count": len(comparable),
                "condition_assumption": grade if grade != "unknown" else "fair_to_like_new",
                "confidence": "very_low",
                "limitations": ["no sufficient matched local comparable prices",
                                "retail price may be stale or for a different variant",
                                "condition is inferred from seller text"],
            }
        return {"status": "unvalued", "reason": "insufficient_verified_comparables",
                "comparable_count": len(comparable), "needed": min_comps}

    base = median(price for price, _ in comparable)
    grade = (listing.get("condition") or {}).get("grade", "unknown")
    if grade == "unknown":
        low_factor, high_factor = CONDITION_FACTORS["fair"], CONDITION_FACTORS["like_new"]
    else:
        factor = CONDITION_FACTORS[grade]
        low_factor, high_factor = factor * 0.83, factor * 1.17
    low = round(base * low_factor * 0.85, 2)
    high = round(base * high_factor * 0.85, 2)
    return {
        "status": "estimated", "currency": "USD",
        "price_type": "asking_based_estimate", "low": low, "high": high,
        "midpoint": round((low + high) / 2, 2),
        "basis": "median comparable asking prices adjusted for stated condition",
        "comparable_count": len(comparable),
        "sources": sorted({comp.get("source") or "unspecified" for _, comp in comparable}),
        "condition_assumption": grade if grade != "unknown" else "fair_to_like_new",
        "negotiation_factor": 0.85,
        "unknown_comparable_conditions": unknown_comp_condition,
        "confidence": "medium" if len(comparable) >= 5 and grade != "unknown" and
        unknown_comp_condition == 0 else "low",
        "limitations": ["asking prices are not sale prices", "condition is inferred from seller text",
                        "location, age, and hidden defects may change value"],
    }


def attach_identity_and_value(listings: list[dict[str, Any]],
                              catalog: list[dict[str, Any]] | None = None,
                              external_comps: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Match a batch and use distinct same-identity listings as comparables."""
    result = []
    comps = list(external_comps or [])
    catalog_by_id = {item["id"]: item for item in (catalog or []) if item.get("id")}
    for original in listings:
        listing = dict(original)
        listing["identity"] = identify_item(listing.get("title", ""), catalog)
        listing["product_candidate"] = suggest_product_candidate(listing.get("title", ""))
        listing["condition"] = infer_condition(
            " ".join((listing.get("title") or "", listing.get("description") or "")))
        result.append(listing)
    for listing in result:
        identity = listing["identity"]
        if identity["id"] and identity["confidence"] >= 0.75:
            comps.append({"source": "facebook_marketplace",
                          "listing_id": listing.get("listing_id"),
                          "item_id": identity["id"],
                          "asking_price": listing.get("asking_price"),
                          "condition": listing["condition"]["grade"],
                          "title": listing.get("title", ""),
                          "search_zip": listing.get("search_zip"),
                          "source_url": listing.get("url")})
        elif furniture_signature(listing.get("title", "")):
            comps.append({"source": "facebook_marketplace",
                          "listing_id": listing.get("listing_id"),
                          "asking_price": listing.get("asking_price"),
                          "condition": listing["condition"]["grade"],
                          "title": listing.get("title", ""),
                          "search_zip": listing.get("search_zip"),
                          "source_url": listing.get("url")})
    for listing in result:
        listing["valuation"] = estimate_value(
            listing, comps, catalog_by_id.get(listing["identity"]["id"]))
    return result
