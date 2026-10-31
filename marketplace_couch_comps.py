"""Export a reviewable, asking-price-only couch comparable pool.

This report never treats a Marketplace asking price as a completed sale.
Only text-checked single-item candidates contribute to the sample statistics.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
from statistics import median

from marketplace_price import seller_asking_price
from marketplace_value import _percentile, furniture_signature, infer_condition


SUBTYPE = (
    ("sectional", re.compile(r"\b(?:sectional|modular|u[ -]?shaped|l[ -]?shaped)\b", re.I)),
    ("loveseat", re.compile(r"\b(?:love[ -]?seat)\b", re.I)),
    ("sofa", re.compile(r"\b(?:couch|sofa|settee)\b", re.I)),
)
BUNDLE = re.compile(r"\b(?:set|bundle|and chairs?|\+\s*chairs?)\b", re.I)
FEATURES = {
    "reclining": re.compile(r"\breclin(?:e|er|ers|ing)\b", re.I),
    "sleeper": re.compile(r"\b(?:sleeper|pull[ -]?out|sofa[ -]?bed|futon)\b", re.I),
    "chaise": re.compile(r"\bchaise\b", re.I),
    "leather": re.compile(r"\bleather\b", re.I),
}
FIELDS = ("listing_id", "subtype", "features", "card_price", "seller_ask",
          "effective_ask", "price_source", "condition", "location",
          "distance_miles_approx", "title", "url", "review_status",
          "review_reasons", "observed_at")


def prepare_candidate(listing: dict, observed_at: str) -> dict:
    title = str(listing.get("title") or "").strip()
    description = str(listing.get("description") or "")
    subtype = next((name for name, pattern in SUBTYPE if pattern.search(title)), "unknown")
    features = [name for name, pattern in FEATURES.items()
                if pattern.search(title + " " + description)]
    card_price = listing.get("asking_price")
    explicit_ask = seller_asking_price(description)
    effective_ask = explicit_ask if card_price == 0 and explicit_ask else card_price
    seller_label = str(listing.get("seller_condition_label") or "")
    reasons = []
    if subtype == "unknown":
        reasons.append("item_type_unclear")
    if (SUBTYPE[1][1].search(title) and SUBTYPE[2][1].search(title)):
        reasons.append("ambiguous_loveseat_or_sofa")
    if BUNDLE.search(title):
        reasons.append("bundle_or_multiple_items")
    if card_price == 0 and explicit_ask is not None:
        reasons.append("zero_card_seller_asks")
    if not isinstance(effective_ask, (int, float)) or effective_ask <= 1:
        reasons.append("free_or_placeholder_price")
    if (explicit_ask is not None and isinstance(card_price, (int, float)) and
            card_price > 0 and explicit_ask != card_price):
        reasons.append("card_and_description_price_conflict")
    if (re.search(r"\bbrand[ -]?new\b", title, re.I) and
            seller_label.casefold().startswith("used")):
        reasons.append("title_and_condition_conflict")
    if listing.get("description_status") not in {"visible", "no_visible_description"}:
        reasons.append("description_unavailable")
    status = "review" if reasons else "candidate"
    condition = infer_condition(" ".join((seller_label, title, description)))["grade"]
    if condition == "unknown" and seller_label:
        condition = seller_label.lower().replace("used - ", "").replace(" ", "_")
    radius = listing.get("radius_check") or {}
    return {
        "listing_id": str(listing.get("listing_id") or ""),
        "subtype": subtype,
        "features": ", ".join(features),
        "card_price": card_price,
        "seller_ask": explicit_ask,
        "effective_ask": effective_ask,
        "price_source": "seller_description" if card_price == 0 and explicit_ask else "facebook_card",
        "condition": condition,
        "location": listing.get("location") or "",
        "distance_miles_approx": radius.get("distance_miles_approx"),
        "title": title,
        "url": listing.get("url") or "",
        "review_status": status,
        "review_reasons": ", ".join(reasons),
        "observed_at": observed_at,
    }


def describe_prices(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    return {"count": len(values),
            "median_asking_price": round(median(values), 2),
            "middle_half_asking_range": [
                round(_percentile(values, 0.25), 2),
                round(_percentile(values, 0.75), 2)]}


def approved_pricing_comparables(input_file: Path) -> list[dict]:
    """Build the frozen, single-item reference pool after price review."""
    data = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("listings"), list):
        raise ValueError("Input must be a Marketplace result with listings")
    zip_code = str(data.get("requested_zip") or "")
    observed_at = str(data.get("captured_at") or "")
    radius = data.get("radius_miles_target")
    result = []
    for listing in data["listings"]:
        row = prepare_candidate(listing, observed_at)
        if row["review_status"] != "candidate" or not furniture_signature(row["title"]):
            continue
        result.append({
            "source": "facebook_marketplace",
            "listing_id": row["listing_id"], "title": row["title"],
            "asking_price": row["effective_ask"], "condition": row["condition"],
            "search_zip": zip_code, "location": row["location"],
            "source_url": row["url"],
            "observed_at": observed_at, "reference_radius_miles": radius,
            "price_reviewed": True,
        })
    return result


def export(input_file: Path, csv_file: Path, summary_file: Path) -> dict:
    data = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("listings"), list):
        raise ValueError("Input must be a Marketplace result with listings")
    rows = [prepare_candidate(item, str(data.get("captured_at") or ""))
            for item in data["listings"]]
    csv_file.parent.mkdir(parents=True, exist_ok=True)
    with csv_file.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    by_type = {}
    for subtype in ("sofa", "loveseat", "sectional"):
        prices = [float(row["effective_ask"]) for row in rows
                  if row["review_status"] == "candidate" and row["subtype"] == subtype]
        used_prices = [float(row["effective_ask"]) for row in rows
                       if row["review_status"] == "candidate" and
                       row["subtype"] == subtype and
                       row["condition"] in {"like_new", "good", "fair"}]
        by_type[subtype] = {"all_conditions": describe_prices(prices),
                            "used_only": describe_prices(used_prices)}
    summary = {
        "source": "facebook_marketplace",
        "price_type": "unverified_local_asking_prices",
        "requested_zip": data.get("requested_zip"),
        "radius_miles": data.get("radius_miles_target"),
        "radius_basis": data.get("radius_filter_method"),
        "observed_at": data.get("captured_at"),
        "cards_in_radius": len(rows),
        "candidate_comparables": sum(row["review_status"] == "candidate" for row in rows),
        "needs_review": sum(row["review_status"] == "review" for row in rows),
        "by_subtype": by_type,
        "limitations": ["Prices are seller asks, not completed sale prices.",
                        "Subtype, condition, and single-item status need human review.",
                        "Seller towns give approximate distances; listing age is unknown."],
    }
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Export couch asking-price comparables")
    parser.add_argument("input", type=Path)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.input, args.csv, args.summary), indent=2))


if __name__ == "__main__":
    main()
