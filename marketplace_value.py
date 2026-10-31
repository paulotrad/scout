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
from marketplace_bundle import extract_bundle
from marketplace_completeness import incomplete_product
from marketplace_mower import (WALK_BEHIND_TYPES, mower_category,
                               mower_peer, mower_variant)


CONDITION_FACTORS = {
    "new": 1.00,
    "like_new": 0.88,
    "good": 0.72,
    "fair": 0.52,
    "needs_repair": 0.35,
    "parts": 0.22,
}


def _words(value: str) -> set[str]:
    separated = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", value.casefold())
    return set(re.findall(r"[a-z0-9]+", separated))


def infer_condition(text: str) -> dict[str, Any]:
    """Infer condition only when the seller's text actually gives a clue."""
    value = text.casefold()
    patterns = (
        ("parts", r"\b(for parts|not working|broken|(?:does\s+not|doesn.?t)\s+(?:work|turn on)|"
         r"(?:stopped|quit|no longer)\s+(?:working|functioning|running|reclining)|"
         r"(?:does\s+not|doesn.?t|won.?t|can.?t|cannot)\s+recline|"
         r"stuck\s+in\s+(?:the\s+)?recline(?:d)?(?:\s+position)?|"
         r"needs?\s+(?:an?\s+)?(?:engine\s+|motor\s+|major\s+)?repair|"
         r"(?:will\s+)?need(?:s)?\s+to\s+be\s+repaired|"
         r"needs?\s+(?:an?\s+|new\s+)?throttle|stuck\s+in\s+fix\s+mode|"
         r"(?:will\s+not|does\s+not|doesn.?t)\s+heat|not\s+heating|heating\s+issue|"
         r"(?:does\s+not|doesn.?t|will\s+not|won.?t)\s+engage|"
         r"(?:can.?t|cannot|won.?t|doesn.?t)\s+(?:get\s+it\s+to\s+)?start|"
         r"(?:not|doesn.?t)\s+run(?:ning)?)\b"),
        ("needs_repair", r"\b((?:hard|difficult)\s+to{1,2}\s+(?:get\s+)?start(?:ed)?|"
         r"needs?\s+to\s+be\s+replaced|"
         r"missing\s+(?:the\s+|a\s+)?(?:bolt|filter)|"
         r"(?:handle\s+is\s+broke(?:n)?|broken\s+handle)|"
         r"needs?\s+(?:a\s+|an\s+)?(?:new\s+)?(?:drive\s+)?belt)\b"),
        ("like_new", r"\b(like new|almost new|mint condition|barely used)\b"),
        ("new", r"\b(brand new(?!\s+(?:(?:front|rear|back|replacement)\s+)?"
         r"(?:carburetor|carb|blade|belt|battery|charger|filter|motor|engine|"
         r"tires?|wheels?|deck|handle|part))|"
         r"new in box|sealed|never used|unopened)\b"),
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
    """Identify a furniture form and price-relevant features named by the seller."""
    candidate = suggest_product_candidate(title)
    furniture = [row["type"] for row in candidate["types"]
                 if row["category"] == "furniture"]
    if candidate["needs_review"] or len(furniture) != 1:
        return None
    product_type = furniture[0]
    text = str(title or "")
    features = []
    if product_type in {"cabinet", "china_cabinet", "file_cabinet",
                        "kitchen_cabinet", "dresser", "nightstand", "desk",
                        "dining_table", "coffee_table", "end_table", "table",
                        "chair", "dining_chair", "accent_chair"}:
        for species in ("cherry", "oak", "walnut", "maple", "mahogany",
                        "teak", "pine"):
            if re.search(rf"\b{species}\b(?!\s+(?:red|colored?|finish))",
                         text, re.I):
                features.append(f"named_{species}")
        if re.search(r"\b(?:handmade|handcrafted|custom[ -]made|artisan[ -]made)\b",
                     text, re.I):
            features.append("handcrafted")
    if product_type in {"sofa", "sectional", "loveseat"}:
        if re.search(r"\bchaise\b", text, re.I):
            features.append("chaise")
        if re.search(r"\b(?:reclining|recliner)\b", text, re.I):
            features.append("reclining")
    return {"product_type": furniture[0],
            "material_clues": candidate["material_clues"],
            "feature_clues": features}


def _furniture_target_signature(listing: dict[str, Any]) -> dict[str, Any] | None:
    """Let a clear photo refine a generic seller title, without guessing peers."""
    titled = furniture_signature(listing.get("title", ""))
    observed = (listing.get("vision") or {}).get("observations") or {}
    if (observed.get("listing_kind") not in {None, "", "product"} or
            observed.get("category") != "furniture" or
            observed.get("category_confidence") != "high"):
        return titled
    pictured = furniture_signature(str(observed.get("item_type") or ""))
    if not pictured:
        return titled
    if not titled:
        return pictured
    if titled["product_type"] == pictured["product_type"]:
        return titled
    broad_to_specific = {
        "chair": {"office_chair", "rocking_chair", "recliner_chair",
                  "dining_chair", "accent_chair"},
        "table": {"dining_table", "coffee_table", "end_table"},
        "cabinet": {"china_cabinet", "file_cabinet", "kitchen_cabinet"},
        "sofa": {"sectional", "loveseat", "sofa_bed"},
    }
    if pictured["product_type"] in broad_to_specific.get(titled["product_type"], set()):
        return {**pictured, "material_clues": titled["material_clues"],
                "feature_clues": titled["feature_clues"]}
    # A specific title and a different specific photo type need a review.
    return None


def general_reference_type(title: str) -> str | None:
    """Return one unambiguous non-furniture form for local price matching."""
    candidate = suggest_product_candidate(title)
    types = [row["type"] for row in candidate["types"] if row["category"] in {
        "appliance", "garden_equipment", "personal_transport", "mobility_equipment"}]
    return types[0] if not candidate["needs_review"] and len(types) == 1 else None


def scooter_form(title: str, description: str = "") -> str:
    """Separate explicitly seated scooters from ordinary scooter price pools."""
    text = f"{title or ''} {description or ''}"
    if re.search(r"\b(?:no seat|without (?:a |the )?seat|seat not included)\b",
                 text, re.I):
        return "stand_up"
    if re.search(r"\b(?:seated|with (?:a |the )?seat|seat included|sit[- ]down|saddle)\b",
                 text, re.I):
        return "seated"
    if re.search(r"\b(?:kick scooter|stand(?:ing)? scooter|foldable scooter|folding scooter)\b",
                 text, re.I):
        return "stand_up"
    return "unknown"


def scooter_model(title: str) -> str | None:
    """Recognize explicit Razor model numbers without guessing from photos."""
    match = re.search(r"\brazor\b.{0,30}\b(e\d{2,3}s?)\b", str(title or ""), re.I)
    return f"razor_{match.group(1).lower()}" if match else None


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    before = int(position)
    after = min(before + 1, len(ordered) - 1)
    return ordered[before] + (ordered[after] - ordered[before]) * (position - before)


PRICE_CATEGORIES = (
    ("best", "Best price"),
    ("good", "Good price"),
    ("average", "Average price"),
    ("slightly_above", "Slightly above"),
    ("high", "High price"),
)


def classify_asking_price(asking_price: Any,
                          comparable_prices: list[float] | None) -> dict | None:
    """Place a seller's ask among comparable *asks*, from a buyer's view."""
    if (isinstance(asking_price, bool) or
            not isinstance(asking_price, (int, float)) or
            not math.isfinite(asking_price) or asking_price < 0):
        return None
    prices = [value for raw in (comparable_prices or [])
              if (value := _money(raw)) is not None]
    if len(prices) < 3:
        return None
    cutoffs = [_percentile(prices, fraction) for fraction in (0.2, 0.4, 0.6, 0.8)]
    if cutoffs[0] == cutoffs[3]:
        # A flat sample does not make its ordinary price a bargain.
        index = 0 if asking_price < cutoffs[0] else 4 if asking_price > cutoffs[3] else 2
        position = (10, 30, 50, 70, 90)[index]
    else:
        index = next((i for i, cutoff in enumerate(cutoffs)
                      if asking_price <= cutoff), 4)
        lower = 0 if index == 0 else cutoffs[index - 1]
        upper = (max(max(prices), asking_price, cutoffs[3] * 1.25)
                 if index == 4 else cutoffs[index])
        within = (asking_price - lower) / (upper - lower) if upper > lower else 0.5
        position = round(max(3, min(97, index * 20 + within * 20)), 1)
    code, label = PRICE_CATEGORIES[index]
    return {
        "code": code, "label": label,
        "seller_asking_price": round(float(asking_price), 2),
        "comparison_count": len(prices),
        "cutoffs": [round(value, 2) for value in cutoffs],
        "bar_position_percent": position,
        "confidence": "medium" if len(prices) >= 8 else "low",
        "basis": "seller ask compared with similar local asking prices",
    }


def _comparable_evidence(matched: list[tuple[float, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Keep a small, price-spanning audit trail for a displayed comparison."""
    ordered = sorted(matched, key=lambda pair: (pair[0], str(pair[1].get("listing_id"))))
    if len(ordered) > 9:
        indexes = sorted({round(index * (len(ordered) - 1) / 8) for index in range(9)})
        ordered = [ordered[index] for index in indexes]
    return [{
        "title": str(comp.get("title") or "Untitled listing")[:160],
        "asking_price": round(price, 2),
        "condition": str(comp.get("condition") or "unknown"),
        "location": str(comp.get("location") or ""),
        "distance_miles": comp.get("reference_distance_miles"),
        "observed_at": comp.get("observed_at"),
        "source": comp.get("source"),
        "source_url": comp.get("source_url"),
    } for price, comp in ordered]


def _furniture_asking_range(listing: dict[str, Any], comps: list[dict[str, Any]],
                            min_comps: int) -> dict[str, Any]:
    """Use local, same-form listings as a cautious asking-price reference."""
    signature = _furniture_target_signature(listing)
    zip_code = listing.get("search_zip")
    if not signature or not zip_code:
        return {"status": "unvalued", "reason": "furniture_type_or_search_zip_unconfirmed"}
    target_materials = set(signature["material_clues"])
    target_features = set(signature["feature_clues"])
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
        # The pricing stage can select a reviewed comp by actual town distance.
        # Older supplied comparables without coordinates still use exact ZIP.
        if comp.get("reference_distance_miles") is None:
            if str(comp.get("search_zip")) != str(zip_code):
                continue
        elif (not isinstance(comp["reference_distance_miles"], (int, float)) or
              comp["reference_distance_miles"] < 0):
            continue
        other = furniture_signature(comp.get("title", ""))
        if not other or other["product_type"] != signature["product_type"]:
            continue
        if target_materials and not target_materials <= set(other["material_clues"]):
            continue
        if target_features and not target_features <= set(other["feature_clues"]):
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
                "comparable_count": len(matched), "needed": min_comps,
                "product_type": signature["product_type"],
                "material_clues_required": sorted(target_materials),
                "feature_clues_required": sorted(target_features)}
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
        "feature_clues_required": sorted(target_features),
        "search_zip": str(zip_code),
        "comparable_count": len(matched),
        "comparable_evidence": _comparable_evidence(matched),
        "asking_price_sample": sorted(prices),
        "sources": sorted({comp["source"] for _, comp in matched}),
        "comparable_urls": [comp.get("source_url") for _, comp in matched
                            if comp.get("source_url")],
        "condition_assumption": target_grade,
        "unknown_comparable_conditions": sum(
            (comp.get("condition") or "unknown") == "unknown"
            for _, comp in matched),
        "reference_distance_miles_max": max(
            (comp.get("reference_distance_miles") or 0) for _, comp in matched),
        "confidence": "low",
        "limitations": limitations,
    }


def _bundle_asking_range(listing: dict[str, Any], bundle: dict,
                         comps: list[dict[str, Any]], min_comps: int) -> dict[str, Any]:
    """Add separate single-item asking ranges; never use another bundle as a peer."""
    components = []
    if bundle["status"] != "confirmed":
        return {"status": "unvalued", "reason": "bundle_quantities_unclear",
                "bundle_components": bundle["components"],
                "bundle_note": bundle["reason"], "comparable_count": 0,
                "needed": min_comps}
    for part in bundle["components"]:
        title = f"{part['material'] or ''} {part['product_type'].replace('_', ' ')}".strip()
        target = {"listing_id": listing.get("listing_id"), "title": title,
                  "search_zip": listing.get("search_zip"),
                  "condition": {"grade": "unknown"}}
        value = (_furniture_asking_range(target, comps, min_comps)
                 if furniture_signature(title) else
                 _general_asking_range(target, comps, min_comps))
        components.append({**part, "status": value["status"],
                           "comparable_count": value.get("comparable_count", 0),
                           "unit_low": value.get("low"),
                           "unit_midpoint": value.get("midpoint"),
                           "unit_high": value.get("high"),
                           "asking_price_sample": value.get("asking_price_sample") or [],
                           "comparable_evidence": value.get("comparable_evidence") or []})
    if any(part["status"] != "provisional" for part in components):
        return {"status": "unvalued", "reason": "insufficient_bundle_component_comparables",
                "bundle_components": components,
                "comparable_count": min(part["comparable_count"] for part in components),
                "needed": min_comps,
                "bundle_ask_scope_unclear": bundle["ask_scope_unclear"]}
    sample_size = min(len(part["asking_price_sample"]) for part in components)
    aggregate = [round(sum(part["quantity"] * part["asking_price_sample"][
        round(index * (len(part["asking_price_sample"]) - 1) / (sample_size - 1))]
        for part in components), 2) for index in range(sample_size)]
    return {"status": "provisional", "currency": "USD",
            "price_type": "itemized_asking_range",
            "low": round(sum(part["quantity"] * part["unit_low"]
                             for part in components), 2),
            "midpoint": round(sum(part["quantity"] * part["unit_midpoint"]
                                  for part in components), 2),
            "high": round(sum(part["quantity"] * part["unit_high"]
                              for part in components), 2),
            "basis": "sum of separate single-item asking prices",
            "bundle_components": components,
            "bundle_ask_scope_unclear": bundle["ask_scope_unclear"],
            "comparable_count": min(part["comparable_count"] for part in components),
            "asking_price_sample": aggregate,
            "confidence": "low",
            "limitations": ["no completed sale prices",
                            "no discount for buying several pieces together is assumed",
                            "each piece's condition may differ"],
            "search_zip": str(listing.get("search_zip") or "")}


def _general_asking_range(listing: dict[str, Any], comps: list[dict[str, Any]],
                          min_comps: int) -> dict[str, Any]:
    """Give a broad, low-confidence median for the same appliance or equipment form."""
    reference_type = general_reference_type(listing.get("title", ""))
    zip_code = listing.get("search_zip")
    if not reference_type or not zip_code:
        return {"status": "unvalued", "reason": "product_form_or_search_zip_unconfirmed"}
    target_grade = (listing.get("condition") or {}).get("grade", "unknown")
    compatible = (WALK_BEHIND_TYPES if reference_type in WALK_BEHIND_TYPES
                  else {reference_type})
    target_variant = mower_variant(listing.get("title", ""),
                                  listing.get("description", ""), reference_type)
    cohort = mower_category(reference_type, target_variant) if target_variant else None
    target_scooter_form = (scooter_form(listing.get("title", ""),
                                        listing.get("description", ""))
                           if reference_type == "electric_scooter" else None)
    target_scooter_model = (scooter_model(listing.get("title", ""))
                            if reference_type == "electric_scooter" else None)
    if target_scooter_form == "seated":
        cohort = {"parent": "personal_transport/electric_scooter",
                  "key": "seated_electric_scooter",
                  "label": "Seated electric scooter",
                   "attributes": {"form": "seated", "model": target_scooter_model}}
    elif target_scooter_model:
        cohort = {"parent": "personal_transport/electric_scooter",
                  "key": target_scooter_model,
                  "label": target_scooter_model.replace("_", " ").title(),
                  "attributes": {"form": target_scooter_form,
                                 "model": target_scooter_model}}
    if reference_type == "electric_scooter" and not target_scooter_model:
        # Kids' and adult scooters span very different price bands. A generic
        # scooter form is insufficient evidence for a deal rating.
        return {"status": "unvalued", "reason": "scooter_variant_details_missing",
                "comparable_count": 0, "needed": min_comps}
    if target_variant and target_variant["power_source"] in {
            "unknown", "electric_unknown", "conflict"}:
        return {"status": "unvalued", "reason": "mower_power_source_unclear",
                "comparable_count": 0, "needed": min_comps,
                "price_cohort": cohort}
    restrict_variant = bool(target_variant and
                            target_variant["power_source"] != "unknown")
    matched = []
    context_rows = []
    seen = set()
    for comp in comps:
        source, comp_id = comp.get("source"), comp.get("listing_id")
        key = (source, str(comp_id))
        if source not in {"facebook_marketplace", "craigslist"} or not comp_id or key in seen:
            continue
        if source == "facebook_marketplace" and str(comp_id) == str(listing.get("listing_id")):
            continue
        if comp.get("reference_distance_miles") is None:
            if str(comp.get("search_zip")) != str(zip_code):
                continue
        elif (not isinstance(comp["reference_distance_miles"], (int, float)) or
              comp["reference_distance_miles"] < 0):
            continue
        other_type = (comp.get("reference_type") or
                      general_reference_type(comp.get("title", "")))
        if other_type not in compatible:
            continue
        mower_form_match = (other_type == reference_type or (
            reference_type == "push_mower" and other_type == "lawn_mower" and
            not re.search(r"\bself[ -]?propel", " ".join((
                str(comp.get("title") or ""),
                str(comp.get("description") or ""))), re.I)))
        if restrict_variant:
            other_variant = mower_variant(comp.get("title", ""),
                                          comp.get("description", ""), other_type)
            if not mower_form_match or not other_variant or not mower_peer(
                    target_variant, other_variant):
                continue
        if other_type == "electric_scooter":
            if target_scooter_model and scooter_model(comp.get("title", "")) != target_scooter_model:
                continue
            other_form = scooter_form(comp.get("title", ""),
                                      comp.get("description", ""))
            if (target_scooter_form == "seated" and other_form != "seated") or (
                    target_scooter_form != "seated" and other_form == "seated"):
                continue
        price = _money(comp.get("asking_price"))
        if price is None:
            continue
        seen.add(key)
        grade = comp.get("condition") or "unknown"
        if grade not in {"parts", "needs_repair"}:
            context_rows.append((price, other_type))
        if not mower_form_match:
            continue
        if target_grade in {"parts", "needs_repair"} and grade != target_grade:
            continue
        if target_grade not in {"unknown", "parts", "needs_repair"} and grade not in {
                "unknown", target_grade}:
            continue
        if target_grade == "unknown" and grade in {"new", "parts", "needs_repair"}:
            continue
        matched.append((price, comp))
    if len(matched) < min_comps:
        if target_scooter_form == "seated":
            return {"status": "unvalued",
                    "reason": "insufficient_same_variant_comparables",
                    "comparable_count": len(matched), "needed": min_comps,
                    "price_cohort": cohort}
        if restrict_variant:
            power = target_variant["power_source"]
            if power in {"electric_unknown", "conflict"}:
                reason = "mower_power_source_unclear"
            elif power == "battery" and (target_variant["brand"] is None or
                                          target_variant["voltage"] is None or
                                          target_variant["deck_inches"] is None or
                                          target_variant["kit"] == "unknown"):
                reason = "mower_variant_details_missing"
            else:
                reason = "insufficient_same_variant_comparables"
            return {"status": "unvalued", "reason": reason,
                    "comparable_count": len(matched), "needed": min_comps,
                    "price_cohort": cohort}
        same_form = [price for price, kind in context_rows if kind == reference_type]
        context = (same_form if len(same_form) >= min_comps else
                   [price for price, _ in context_rows])
        snapshot = ({"midpoint": round(median(context), 2),
                     "low": round(_percentile(context, 0.25), 2),
                     "high": round(_percentile(context, 0.75), 2),
                     "comparable_count": len(context),
                     "basis": ("same product form" if len(same_form) >= min_comps else
                               "mixed walk-behind mower forms")}
                    if len(context) >= min_comps else None)
        return {"status": "unvalued", "reason": "insufficient_local_product_comparables",
                "comparable_count": len(matched), "needed": min_comps,
                **({"price_cohort": cohort} if cohort else {}),
                **({"asking_context": snapshot} if snapshot else {})}
    prices = sorted(price for price, _ in matched)
    return {
        "status": "provisional", "currency": "USD", "price_type": "local_asking_range",
        "low": round(_percentile(prices, 0.25), 2),
        "high": round(_percentile(prices, 0.75), 2),
        "midpoint": round(median(prices), 2),
        "basis": ("median asking price of nearby seated electric scooters"
                  if target_scooter_form == "seated" else
                  "median asking price of nearby listings in the same "
                  "power, form, size, and kit cohort" if restrict_variant else
                  "median asking price of nearby listings for the same product form"),
        "product_type": reference_type, "search_zip": str(zip_code),
        **({"price_cohort": cohort} if cohort else {}),
        "comparable_count": len(matched), "asking_price_sample": prices,
        "comparable_evidence": _comparable_evidence(matched),
        "sources": sorted({comp["source"] for _, comp in matched}),
        "condition_assumption": target_grade,
        "unknown_comparable_conditions": sum(
            (comp.get("condition") or "unknown") == "unknown"
            for _, comp in matched),
        "reference_distance_miles_max": max(
            (comp.get("reference_distance_miles") or 0) for _, comp in matched),
        "confidence": "low",
        "limitations": ["asking prices are not completed sale prices",
                        "brand, model, size, age, and hidden damage may differ"],
    }


def estimate_value(listing: dict[str, Any], comps: list[dict[str, Any]],
                   catalog_item: dict[str, Any] | None = None,
                   min_comps: int = 3) -> dict[str, Any]:
    """Estimate a broad range from other listings for the exact same item ID.

    Comparables are asking prices. Condition adjustments and a 15% negotiation
    discount are explicit assumptions; the output is not a sale-price prediction.
    """
    if "pet_listing" in ((listing.get("vision") or {}).get("review_flags") or []):
        return {"status": "unvalued", "reason": "pet_listing"}
    if issue := incomplete_product(listing):
        return {"status": "unvalued", **issue, "comparable_count": 0,
                "needed": min_comps}
    bundle = extract_bundle(listing)
    comps = [comp for comp in comps if incomplete_product(comp) is None and
             extract_bundle(comp) is None]
    if bundle:
        return _bundle_asking_range(listing, bundle, comps, min_comps)
    identity = listing.get("identity") or {}
    item_id = identity.get("id")
    if identity.get("category") == "vehicle":
        return {"status": "unvalued", "reason": "vehicle_needs_mileage_title_and_history"}
    if not item_id or identity.get("confidence", 0) < 0.75:
        if _furniture_target_signature(listing):
            return _furniture_asking_range(listing, comps, min_comps)
        if general_reference_type(listing.get("title", "")):
            return _general_asking_range(listing, comps, min_comps)
        observations = (listing.get("vision") or {}).get("observations") or {}
        known_type = (observations.get("item_type") and
                      observations.get("category_confidence") in {"medium", "high"} and
                      not set((listing.get("vision") or {}).get("review_flags") or [])
                      .intersection({"seller_title_model_type_disagree",
                                     "item_identity_conflict"}))
        return {"status": "unvalued", "reason": (
            "price_group_unavailable" if known_type else "item_identity_unconfirmed")}

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
        if general_reference_type(listing.get("title", "")):
            local = _general_asking_range(listing, comps, min_comps)
            if (local.get("status") == "provisional" or
                    local.get("price_cohort") is not None or
                    local.get("reason") == "scooter_variant_details_missing"):
                return local
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
    asking_factor = CONDITION_FACTORS.get(grade, CONDITION_FACTORS["good"])
    return {
        "status": "estimated", "currency": "USD",
        "price_type": "asking_based_estimate", "low": low, "high": high,
        "midpoint": round((low + high) / 2, 2),
        "basis": "median comparable asking prices adjusted for stated condition",
        "comparable_count": len(comparable),
        "comparable_evidence": _comparable_evidence([
            (price * asking_factor, comp) for price, comp in comparable]),
        "asking_price_sample": sorted(round(price * asking_factor, 2)
                                      for price, _ in comparable),
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
