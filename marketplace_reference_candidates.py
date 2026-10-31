"""Screen local asking-price listings before they become price references."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
from statistics import median

from marketplace_price import seller_asking_price
from marketplace_bundle import extract_bundle
from marketplace_completeness import incomplete_product
from marketplace_value import _percentile, infer_condition
from marketplace_vision import seller_offer_override
from product_candidate import suggest_product_candidate


TARGET_TYPES = {
    "refrigerator", "washer", "dryer", "washer_dryer_combo", "dishwasher",
    "stove", "freezer", "microwave", "cabinet", "china_cabinet",
    "file_cabinet", "kitchen_cabinet", "dining_table", "coffee_table",
    "end_table", "table", "chair", "office_chair", "rocking_chair",
    "recliner_chair", "dining_chair", "sofa", "loveseat", "sectional",
    "sofa_bed", "dresser", "nightstand", "ottoman",
    "accent_chair", "lawn_mower", "riding_mower", "push_mower",
    "zero_turn_mower", "commercial_walk_behind_mower", "self_propelled_mower",
    "reel_mower", "robot_mower",
    "electric_scooter", "mobility_scooter",
}
BUNDLE = re.compile(
    r"\b(?:bundle|lot of|set of|pair of|multiple (?:items|units)|"
    r"for both|for all|\d+\s+for\s+\$?\d+|"
    r"(?:with|w/)\s*accessories|"
    r"(?:comes with|includes?)\s+\d+\s+chairs?|"
    r"table (?:with|and|\+) \d* chairs?|washer (?:and|\+|/) dryer|"
    r"chair (?:with|and|\+) ottoman|"
    r"riding\s+mower\s+with\s+bagger|"
    r"comes\s+with\s+(?:a\s+|\d+\s*(?:place|bin)\s+)?bagger)\b", re.I
)
ACCESSORY = re.compile(r"\b(?:charger only|battery only|parts only|for parts|"
                       r"replacement parts?|stove items?|no deck|missing deck|without deck|"
                       r"repair service|pickup service|wanted|iso)\b", re.I)
SERVICE = re.compile(r"\b(?:we (?:buy|haul|remove)|removal service|pickup service|"
                     r"installation service|repair service|discount|storewide|"
                     r"trade[ -]?in|bring\s+your\b.{0,80}\b(?:off|credit)\b)\b", re.I)
DEALER = re.compile(r"\b(?:finance available|financing available|cash purchases|"
                    r"shop quality used|appliance deals|months? warranty|"
                    r"warranty included|store location)\b", re.I)
MECHANICAL_ISSUE = re.compile(
    r"\b(?:(?:needs?|might need|may need)\s+(?:a\s+|an\s+)?(?:new\s+)?"
    r"(?:(?:drive|deck)\s+)?(?:belt|battery|engine|motor|charger|throttle|lid lock|door latch)|"
    r"no\s+(?:engine|battery|charger|deck)|"
    r"(?:missing|lost|loss|without)\s+(?:the\s+|a\s+)?"
    r"(?:engine|battery|charger|shroud|coils|bolt|filter)|"
    r"(?:will not|won.?t|does not|doesn.?t|currently does not)\s+"
    r"(?:engage|start|run|work|heat|turn off|turn on)|"
    r"(?:could\s+not|couldn.?t|can.?t)\s+get\s+it\s+started|"
    r"heating issue|not heating|doesn.?t fully latch|leak(?:s|ing)?\b|"
    r"stuck\s+in\s+fix\s+mode|(?:will\s+)?need(?:s)?\s+to\s+be\s+(?:repaired|replaced)|"
    r"(?:hard|difficult)\s+to{1,2}\s+(?:get\s+)?start(?:ed)?|"
    r"(?:handle\s+is\s+broke(?:n)?|broken\s+handle)|"
    r"(?:don.?t|do\s+not)\s+have\s+(?:is\s+)?(?:the\s+)?"
    r"(?:belt\s+and\s+pulley\s+)?cover|"
    r"needs?\s+(?:a\s+)?(?:little\s+)?work\s+on\s+the\s+deck|"
    r"needs?\s+(?:some\s+)?(?:tlc|work)|"
    r"needs?\s+(?:an?\s+|new\s+)?(?:blades?|carb(?:uretor)?|gearbox|drive)|"
    r"carb(?:uretor)?\s+needs?\s+clean(?:ed|ing)?|"
    r"(?:deck|frame)\b.{0,35}\brust(?:ed|ing)?(?:\s+out)?|"
    r"(?:handle\s+broke(?:n)?|handle\s+likes\s+to\s+not\s+stay|"
    r"rig\s+the\s+handle|funny\s+to\s+start|sometimes\s+runs)|"
    r"(?:does\s+not|doesn.?t|will\s+not|won.?t)\s+self[ -]?propel|"
    r"(?:drive|gearbox)\s+(?:issue|repair)|stopped\s+working|"
    r"(?:will\s+not\s+stay\s+running|stall(?:s|ing)?\s+out|"
    r"not\s+rev\s+correctly|"
    r"starts?\s+and\s+then\s+shuts?\s+down|currently\s+not\s+starting)|"
    r"needs?\s+(?:some\s+)?love|"
    r"need\s+a\s+jump\s+pack\s+to\s+start|"
    r"needs?\s+to\s+be\s+jump[ -]?started|"
    r"needs?\s+.{0,30}\bweld(?:ed|ing)?\b|"
    r"needs?\s+(?:a\s+)?tune[ -]?up|needs?\s+replaced|"
    r"(?:plastic\s+is\s+missing|spindle\s+bearing\s+seized)|"
    r"(?:don.?t|do\s+not)\s+know\s+if\s+it\s+starts|"
    r"battery\s+is\s+dead|"
    r"(?:belt\s+is\s+loose|spindle\s+is\s+binding)|"
    r"muffler\s+(?:fell|feel)\s+off|"
    r"(?:do\s+not|don.?t)\s+have\s+(?:a\s+|the\s+)?"
    r"(?:batter(?:y|ies)|charger)|"
    r"needs?\s+attention|rust(?:ed)?\s+off|"
    r"(?:believe|belive|belve)\s+it\s+runs|"
    r"haven.?t\s+ran|ran\s+when\s+(?:i\s+)?parked|"
    r"needs?\s+to\s+be\s+tuned|"
    r"(?:will\s+)?needs?\s+(?:an?\s+|new\s+)?(?:rear\s+)?tire|"
    r"(?:will\s+)?need\s+an?\s+oil\s+change|"
    r"(?:mow(?:ing)?\s+can.?t\s+happen|will\s+not\s+stay\s+level))\b", re.I)
PLURAL_ITEM = re.compile(r"\b(?:cabinets|chairs|tables|nightstands|night\s+stands|refrigerators|fridges|"
                         r"washers|dryers|stoves|mowers|scooters|wardrobes|"
                         r"armoires)\b", re.I)
FIELDS = ("listing_id", "reference_type", "effective_ask", "card_price",
          "seller_ask", "condition", "location", "title", "description",
          "review_status", "review_reasons", "url", "observed_at")


def screen_listing(listing: dict, observed_at: str) -> dict:
    title = str(listing.get("title") or "").strip()
    description = str(listing.get("description") or "")
    searchable_description = description.replace("+", " ")
    candidate = suggest_product_candidate(title)
    types = [row["type"] for row in candidate["types"] if row["type"] in TARGET_TYPES]
    reasons = []
    if len(types) != 1 or candidate["needs_review"]:
        reasons.append("item_type_unclear_or_multiple")
    seller_form = re.search(
        r"\bLawn\s+Mower\s+Type\s+(Self\s+Propelled\s+Mower|"
        r"Push\s+Mower|Riding\s+Mower|Zero\s+Turn\s+Mower)\b",
        re.sub(r"\s+", " ", searchable_description), re.I)
    if seller_form:
        declared = {
            "self propelled mower": "self_propelled_mower",
            "push mower": "push_mower", "riding mower": "riding_mower",
            "zero turn mower": "zero_turn_mower",
        }[seller_form.group(1).lower()]
        if types == ["lawn_mower"]:
            types = [declared]
        elif len(types) == 1 and types[0] != declared:
            reasons.append("seller_type_conflicts_with_title")
    if types == ["riding_mower"] and re.search(
            r"\bzero[ -]?turn\s+(?:riding\s+)?(?:lawn\s*)?mower\b",
            searchable_description, re.I):
        types = ["zero_turn_mower"]
    if BUNDLE.search(title + " " + searchable_description):
        reasons.append("bundle_or_multiple_items")
    if extract_bundle(listing):
        reasons.append("bundle_or_multiple_items")
    if PLURAL_ITEM.search(title):
        reasons.append("multiple_items_in_title")
    if ACCESSORY.search(title):
        reasons.append("accessory_or_parts_listing")
    if SERVICE.search(title + " " + searchable_description):
        reasons.append("service_or_promotion")
    if seller_offer_override(title, description):
        reasons.append("service_or_promotion")
    if DEALER.search(title + " " + searchable_description):
        reasons.append("dealer_or_warranty_listing")
    if MECHANICAL_ISSUE.search(title + " " + searchable_description):
        reasons.append("mechanical_issue_needs_review")
    if incomplete_product(listing):
        reasons.append("missing_major_parts_or_parts_only")
    if types == ["push_mower"] and re.search(
            r"\b(?:has|with)\s+self[ -]?propelled\b", searchable_description,
            re.I) and not re.search(r"\bnot\s+self[ -]?propelled\b",
                                    searchable_description, re.I):
        reasons.append("seller_type_conflicts_with_title")
    observations = (listing.get("vision") or {}).get("observations") or {}
    if observations.get("listing_kind") not in {None, "", "product"}:
        reasons.append("not_a_physical_item")
    card = listing.get("asking_price")
    seller = seller_asking_price(description)
    effective = seller if card == 0 and seller is not None else card
    if card == 0 and seller is not None:
        reasons.append("zero_card_seller_asks")
    if (isinstance(card, (int, float)) and card > 0 and seller is not None
            and abs(card - seller) > 0.01):
        reasons.append("card_and_description_price_conflict")
    if isinstance(card, (int, float)) and card > 0:
        other_amounts = [float(raw.replace(",", "")) for groups in re.findall(
            r"\$\s*(\d[\d,]*(?:\.\d{1,2})?)\b|\b(\d[\d,]*)\s*(?:obo|or\s+best\s+offer)\b",
            description, re.I) for raw in groups if raw]
        if any(abs(amount - card) > 0.01 for amount in other_amounts):
            reasons.append("other_amount_in_seller_description")
    if not isinstance(effective, (int, float)) or effective <= 1:
        reasons.append("free_or_placeholder_price")
    if effective in {1234, 12345, 123456}:
        reasons.append("likely_placeholder_price")
    if listing.get("description_status") != "visible":
        reasons.append("description_unavailable")
    if (listing.get("radius_check") or {}).get("status") != "included":
        reasons.append("outside_or_unconfirmed_radius")
    condition = infer_condition(" ".join((
        str(listing.get("seller_condition_label") or ""), title,
        searchable_description)))["grade"]
    if condition == "unknown" and listing.get("seller_condition_label"):
        condition = str(listing["seller_condition_label"]).lower().replace(
            "used - ", "").replace(" ", "_")
    if condition == "parts":
        reasons.append("repair_or_parts_condition")
    return {
        "listing_id": str(listing.get("listing_id") or ""),
        "reference_type": types[0] if len(types) == 1 else "unknown",
        "effective_ask": effective,
        "card_price": card,
        "seller_ask": seller,
        "condition": condition,
        "location": str(listing.get("location") or ""),
        "title": title,
        "description": description[:2000],
        "review_status": "candidate" if not reasons else "review",
        "review_reasons": ", ".join(dict.fromkeys(reasons)),
        "url": str(listing.get("url") or ""),
        "observed_at": observed_at,
    }


def build(input_file: Path, output_json: Path, output_csv: Path) -> dict:
    source = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(source, dict) or not isinstance(source.get("listings"), list):
        raise ValueError("Expected a Marketplace run with listings")
    observed_at = str(source.get("captured_at") or "")
    rows = [screen_listing(item, observed_at) for item in source["listings"]]
    by_type = {}
    for reference_type in sorted(TARGET_TYPES):
        prices = [float(row["effective_ask"]) for row in rows
                  if row["reference_type"] == reference_type and
                  row["review_status"] == "candidate"]
        if prices:
            by_type[reference_type] = {
                "candidate_count": len(prices),
                "median_asking_price": round(median(prices), 2),
                "middle_half_asking_range": [
                    round(_percentile(prices, 0.25), 2),
                    round(_percentile(prices, 0.75), 2)],
            }
    report = {
        "source_file": str(input_file), "requested_zip": source.get("requested_zip"),
        "radius_miles": source.get("radius_miles_target"),
        "observed_at": observed_at,
        "price_basis": "seller_asking_price_not_completed_sale",
        "reviewed_for_estimation": False,
        "candidate_count": sum(row["review_status"] == "candidate" for row in rows),
        "needs_review_count": sum(row["review_status"] == "review" for row in rows),
        "by_type": by_type, "listings": rows,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return report


def approved_pricing_comparables(report: dict,
                                exclude_ids: set[str] | None = None) -> list[dict]:
    """Freeze screened single-item asks as local provisional references."""
    zip_code = str(report.get("requested_zip") or "")
    observed_at = str(report.get("observed_at") or "")
    radius = report.get("radius_miles")
    if not zip_code or not observed_at or not isinstance(report.get("listings"), list):
        raise ValueError("Reference report is missing location, time, or listings")
    rows = []
    excluded = exclude_ids or set()
    for item in report["listings"]:
        if (item.get("review_status") != "candidate" or
                str(item.get("listing_id")) in excluded):
            continue
        rows.append({
            "source": "facebook_marketplace", "listing_id": item["listing_id"],
            "title": item["title"], "reference_type": item["reference_type"],
            "asking_price": item["effective_ask"], "condition": item["condition"],
            "search_zip": zip_code, "location": item["location"],
            "source_url": item["url"], "observed_at": observed_at,
            "reference_radius_miles": radius, "price_reviewed": True,
            "review_basis": "screened_seller_title_description_and_price",
            "price_basis": "seller_asking_price_not_completed_sale",
        })
    if len({item["listing_id"] for item in rows}) != len(rows):
        raise ValueError("Duplicate listing in screened reference pool")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Screen local asking-price references")
    parser.add_argument("input", type=Path)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--approved", type=Path,
                        help="Write screened asking-price references to the live pool")
    parser.add_argument("--exclude-approved", type=Path,
                        help="Keep listings from an existing pool out of this new pool")
    args = parser.parse_args()
    result = build(args.input, args.json, args.csv)
    if args.approved:
        excluded = set()
        if args.exclude_approved:
            existing = json.loads(args.exclude_approved.read_text(encoding="utf-8"))
            excluded = {str(row["listing_id"]) for row in existing}
        approved = approved_pricing_comparables(result, excluded)
        args.approved.parent.mkdir(parents=True, exist_ok=True)
        args.approved.write_text(json.dumps(approved, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
        result["approved_count"] = len(approved)
    print(json.dumps({"candidate_count": result["candidate_count"],
                      "needs_review_count": result["needs_review_count"],
                      "by_type": result["by_type"]}, indent=2))


if __name__ == "__main__":
    main()
