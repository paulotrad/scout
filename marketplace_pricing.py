"""Apply reviewed local asking prices to finished Marketplace scans."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from marketplace_atomic import write_json
from marketplace_bundle import extract_bundle
from marketplace_completeness import incomplete_product
from marketplace_listing_copy import mixed_item_lot
from marketplace_db import DEFAULT_DB, store_result
from marketplace_price import display_price, seller_asking_price
from marketplace_radius import distance_miles
from marketplace_value import (_furniture_target_signature, classify_asking_price,
                               estimate_value, general_reference_type,
                                infer_condition, scooter_model)
from marketplace_vision import seller_offer_override


ROOT = Path(__file__).resolve().parent
APPROVED_COUCH_POOL = ROOT / "runs/couch_comparables_21009_30mi_approved.json"
GEOCODE_CACHE = ROOT / "runs/town_geocodes.json"
GRADES = {"new", "like_new", "good", "fair", "needs_repair", "unknown"}
PRICE_SCOPES_MILES = (30, 60, 120, 250, 500)
LOCAL_PRICE_SCOPES_MILES = PRICE_SCOPES_MILES[:2]
MAX_PRICE_AGE = timedelta(days=365)


def _price_scopes(listing: dict) -> tuple[int, ...]:
    """Broaden sparse mower cohorts without treating furniture as national comps."""
    if extract_bundle(listing):
        return LOCAL_PRICE_SCOPES_MILES
    kind = general_reference_type(str(listing.get("title") or ""))
    if kind in {"lawn_mower", "push_mower", "self_propelled_mower"}:
        return PRICE_SCOPES_MILES
    if kind and "mower" in kind:
        return PRICE_SCOPES_MILES[:-1]
    if kind == "electric_scooter" and scooter_model(str(listing.get("title") or "")):
        return PRICE_SCOPES_MILES
    return LOCAL_PRICE_SCOPES_MILES


def _scope_label(miles: int) -> str:
    return "local" if miles <= 60 else "regional" if miles <= 250 else "broader area"


def _condition_for_pricing(listing: dict) -> tuple[str, str]:
    seller = infer_condition(" ".join((str(listing.get("title") or ""),
                                       str(listing.get("description") or ""))))
    if seller["grade"] in {"parts", "needs_repair"}:
        return seller["grade"], "seller_reports_repair_or_nonfunction"
    couch = listing.get("couch_quality") or {}
    if couch.get("status") == "estimated":
        guarded = couch.get("guardrails") or {}
        grade = guarded.get("guarded_grade")
        confidence = guarded.get("guarded_confidence")
        if (guarded.get("condition_cohort_eligible") is True and
                grade in {"new", "like_new", "good", "fair"} and
                confidence in {"medium", "high"}):
            return grade, "guarded_couch_quality"
        if grade == "poor":
            return "unknown", "couch_quality_poor_no_cohort"
        return "unknown", "couch_quality_requires_review"
    assessment = listing.get("condition_assessment") or {}
    result = assessment.get("result") or {}
    grade = result.get("condition_grade")
    if (assessment.get("status") == "estimated" and grade in GRADES and
            result.get("confidence") in {"medium", "high"}):
        return grade, "guarded_image_and_seller_assessment"
    grade = (listing.get("condition") or {}).get("grade")
    return (grade if grade in GRADES else "unknown"), "seller_text_or_unknown"


def _load_pool(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Reviewed price pool must be an array of listings")
    if any(not row.get("price_reviewed") or not row.get("location")
           for row in rows):
        raise ValueError("Reviewed price pool has unapproved or unlocated rows")
    return rows


def _with_saved_descriptions(rows: list[dict], db_path: Path | None) -> list[dict]:
    """Use saved seller text to distinguish battery, gas, and kit variants."""
    if db_path is None or not db_path.is_file():
        return rows
    ids = list(dict.fromkeys(str(row.get("listing_id")) for row in rows
                             if row.get("source") == "facebook_marketplace" and
                             row.get("listing_id") and not row.get("description")))
    if not ids:
        return rows
    descriptions = {}
    try:
        with closing(sqlite3.connect(db_path)) as db:
            for offset in range(0, len(ids), 500):
                group = ids[offset:offset + 500]
                found = db.execute(
                    "SELECT listing_id, description FROM listings "
                    "WHERE source = ? AND listing_id IN (" +
                    ",".join("?" for _ in group) + ")",
                    ("facebook_marketplace", *group))
                descriptions.update((str(item_id), description or "")
                                    for item_id, description in found)
    except sqlite3.Error:
        return rows
    return [{**row, "description": descriptions.get(str(row.get("listing_id")), "")}
            if row.get("source") == "facebook_marketplace" and
            not row.get("description") else row for row in rows]


def _observation_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return observed.astimezone(timezone.utc) if observed.tzinfo else None


def _recent_price(row: dict, as_of: datetime) -> bool:
    observed = _observation_time(row.get("observed_at"))
    return observed is not None and timedelta(0) <= as_of - observed <= MAX_PRICE_AGE


def _coordinates(cache: dict, place: str) -> tuple[float, float] | None:
    found = cache.get(place)
    if not isinstance(found, dict):
        return None
    try:
        return float(found["latitude"]), float(found["longitude"])
    except (KeyError, TypeError, ValueError):
        return None


def _nearby_pool(rows: list[dict], cache: dict, center_zip: str,
                 miles: int) -> list[dict]:
    center = _coordinates(cache, center_zip)
    if center is None:
        return []
    result = []
    for row in rows:
        place = _coordinates(cache, row["location"])
        if place is None:
            continue
        distance = distance_miles(center, place)
        if distance <= miles:
            result.append({**row, "reference_distance_miles": round(distance, 2)})
    return result


def _effective_seller_ask(listing: dict) -> tuple[int | float | None, str | None]:
    card = listing.get("asking_price")
    description = str(listing.get("description") or "")
    stated = seller_asking_price(description)
    if (isinstance(card, (int, float)) and not isinstance(card, bool) and
            card > 0 and stated is not None and stated != card):
        return None, "card_and_description_price_conflict"
    flags = ((listing.get("vision") or {}).get("review_flags") or []) + (
        (listing.get("condition_assessment") or {}).get("review_flags") or [])
    effective, source, _ = display_price(card, description, flags,
                                         str(listing.get("title") or ""))
    return (effective, None) if source != "unconfirmed" else (None, "price_unconfirmed")


def run(input_file: Path, output_file: Path,
        pool_file: Path | None = None,
        db_path: Path | None = DEFAULT_DB,
        geocode_cache: Path = GEOCODE_CACHE,
        reference_time: datetime | None = None) -> dict:
    data = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("listings"), list):
        raise ValueError("Input must have a listings array")
    count = 0
    as_of = reference_time or _observation_time(data.get("captured_at")) or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise ValueError("Price reference time must include a timezone")
    as_of = as_of.astimezone(timezone.utc)
    pool_files = ([pool_file] if pool_file is not None else
                  sorted((ROOT / "runs").glob("*_approved.json")))
    all_prices = [row for path in pool_files if path.is_file()
                  for row in _load_pool(path)]
    all_prices = _with_saved_descriptions(all_prices, db_path)
    recent_prices = [row for row in all_prices if _recent_price(row, as_of)]
    complete_prices = [row for row in recent_prices if incomplete_product(row) is None]
    single_prices = [row for row in complete_prices if not mixed_item_lot(
        row.get("title") or "", row.get("description") or "")]
    pool = [row for row in single_prices if extract_bundle(row) is None and
            not seller_offer_override(row.get("title") or "",
                                      row.get("description") or "")]
    for listing in data["listings"]:
        if mixed_item_lot(listing.get("title") or "",
                          listing.get("description") or ""):
            listing["valuation"] = {"status": "unvalued", "reason": "mixed_item_lot",
                                    "comparable_count": 0}
        elif issue := incomplete_product(listing):
            listing["valuation"] = {"status": "unvalued", **issue,
                                    "comparable_count": 0, "needed": 3}
        elif extract_bundle(listing):
            listing["valuation"] = estimate_value(
                {**listing, "search_zip": str(data.get("requested_zip") or "")},
                [], min_comps=3)
    cache = (json.loads(geocode_cache.read_text(encoding="utf-8"))
             if geocode_cache.is_file() else {})
    center_zip = str(data.get("requested_zip") or "")
    if pool and _coordinates(cache, center_zip):
        nearby = {miles: _nearby_pool(pool, cache, center_zip, miles)
                  for miles in PRICE_SCOPES_MILES}
        for listing in data["listings"]:
            if seller_offer_override(listing.get("title") or "",
                                     listing.get("description") or ""):
                listing["valuation"] = {"status": "unvalued",
                                        "reason": "not_single_item_offer"}
                continue
            if mixed_item_lot(listing.get("title") or "",
                              listing.get("description") or ""):
                continue
            if incomplete_product(listing):
                continue
            if not extract_bundle(listing) and not _furniture_target_signature(listing) and not (
                    general_reference_type(str(listing.get("title") or ""))) and not (
                    listing.get("identity") or {}).get("id"):
                continue
            observations = (listing.get("vision") or {}).get("observations") or {}
            if observations.get("listing_kind") not in {None, "", "product"}:
                continue
            grade, condition_source = _condition_for_pricing(listing)
            target = {**listing, "search_zip": center_zip,
                      "condition": {"grade": grade}}
            scopes = _price_scopes(listing)
            for miles in scopes:
                valuation = estimate_value(target, nearby[miles])
                if valuation.get("status") in {"estimated", "provisional"}:
                    break
                if valuation.get("reason") in {
                        "mower_power_source_unclear", "mower_variant_details_missing"}:
                    # More geography cannot supply a missing power or kit fact.
                    break
            valuation["reference_scope_miles"] = miles
            valuation["reference_scope_label"] = _scope_label(miles)
            if valuation.get("status") in {"estimated", "provisional"}:
                if miles > 60 and valuation.get("price_type") != "itemized_asking_range":
                    valuation["price_type"] = "broader_area_asking_range"
                    valuation["basis"] = (
                        "median asking price of compatible listings across a broader area")
                    valuation.setdefault("limitations", []).append(
                        "broader-area asking prices may differ from local demand")
                valuation.update({
                    "reference_observed_at": max(
                        (row.get("observed_at") or "") for row in nearby[miles]),
                    "reviewed_pool_size": len(pool),
                    "condition_source": condition_source,
                })
                ask, issue = _effective_seller_ask(listing)
                quality_issue = (condition_source if condition_source in {
                    "couch_quality_requires_review", "couch_quality_poor_no_cohort"} else None)
                second = listing.get("second_opinion") or {}
                if second.get("status") == "reviewed" and second.get("review_required"):
                    quality_issue = ("second_opinion_item_type_conflict"
                                     if second.get("item_type_agrees") is False else
                                     "second_opinion_condition_conflict")
                category = (classify_asking_price(ask, valuation.get("asking_price_sample"))
                            if quality_issue is None and not valuation.get(
                                "bundle_ask_scope_unclear") else None)
                if category:
                    if valuation.get("price_type") == "itemized_asking_range":
                        category["confidence"] = "low"
                        category["basis"] = (
                            "seller ask compared with a sum of separate item asking prices")
                    if miles > 60:
                        category["confidence"] = "low"
                        category["basis"] = (
                            "seller ask compared with compatible broader-area asking prices")
                    if (valuation.get("condition_assumption") == "unknown" or
                            valuation.get("unknown_comparable_conditions", 0)):
                        category["confidence"] = "low"
                    valuation["price_category"] = category
                elif issue or quality_issue or valuation.get("bundle_ask_scope_unclear"):
                    valuation["price_category_reason"] = (
                        issue or quality_issue or "bundle_asking_price_scope_unclear")
                count += 1
                listing["valuation"] = valuation
            elif valuation.get("bundle_components") is not None:
                listing["valuation"] = valuation
            elif (listing.get("couch_quality") or {}).get("status") == "estimated":
                listing["valuation"] = {
                    **valuation, "condition_source": condition_source,
                    "price_category_reason": (
                        "insufficient_condition_comparables"
                        if condition_source == "guarded_couch_quality" else condition_source),
                }
            else:
                # Repricing must revoke an old rating when stronger matching
                # rules or expired references make its evidence insufficient.
                listing["valuation"] = valuation
        data["reviewed_pricing"] = {
            "status": "applied", "search_zip": center_zip,
            "reference_as_of": as_of.isoformat(),
            "available_scopes_miles": list(PRICE_SCOPES_MILES),
            "pool_size": len(pool),
            "nearby_pool_size": len(nearby[LOCAL_PRICE_SCOPES_MILES[-1]]),
            "broader_pool_size": len(nearby[PRICE_SCOPES_MILES[-1]]),
            "older_or_future_prices_excluded": len(all_prices) - len(recent_prices),
            "incomplete_references_excluded": len(recent_prices) - len(complete_prices),
            "mixed_lot_references_excluded": len(complete_prices) - len(single_prices),
            "bundle_references_excluded": len(single_prices) - len(pool),
            "price_window_days": 365, "estimated_items": count,
        }
    else:
        for listing in data["listings"]:
            if (listing.get("valuation") or {}).get("status") in {
                    "estimated", "provisional"}:
                listing["valuation"] = {
                    "status": "unvalued", "reason": "price_group_unavailable",
                    "comparable_count": 0, "needed": 3}
        data["reviewed_pricing"] = {
            "status": ("pool_missing" if not all_prices else
                       "no_prices_in_last_year" if not pool else
                       "search_zip_coordinates_missing"),
            "reference_as_of": as_of.isoformat(),
            "older_or_future_prices_excluded": len(all_prices) - len(recent_prices),
            "incomplete_references_excluded": len(recent_prices) - len(complete_prices),
            "mixed_lot_references_excluded": len(complete_prices) - len(single_prices),
            "bundle_references_excluded": len(single_prices) - len(pool),
            "price_window_days": 365,
            "estimated_items": 0,
        }
    data["reviewed_pricing_at"] = datetime.now(timezone.utc).isoformat()
    output_file.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_file, data)
    if db_path is not None:
        stored = store_result(data, input_file.parent, db_path)
        data["database_file"] = stored["database_file"]
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply reviewed local asking prices")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pool", type=Path,
                        help="Use one reviewed pool instead of all approved pools")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--geocode-cache", type=Path, default=GEOCODE_CACHE)
    parser.add_argument("--reference-as-of-now", action="store_true",
                        help="Reprice an older scan using references available now")
    args = parser.parse_args()
    result = run(args.input, args.output, args.pool, args.db, args.geocode_cache,
                 datetime.now(timezone.utc) if args.reference_as_of_now else None)
    print(json.dumps(result["reviewed_pricing"], indent=2))


if __name__ == "__main__":
    main()
