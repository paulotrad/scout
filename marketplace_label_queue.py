"""Export Marketplace matches for human correction before training a model."""

import argparse
import csv
import json
from pathlib import Path


FIELDS = (
    "listing_id", "title", "description", "asking_price", "seller_location", "search_zip",
    "proposed_item_id", "verified_item_id", "proposed_condition",
    "verified_condition", "notes", "image_file", "vision_model",
    "vision_primary_object", "vision_visible_brand", "vision_visible_model",
    "vision_wear_or_damage", "vision_uncertainties", "verified_item_type",
    "verified_material", "vision_category", "vision_item_type",
    "vision_category_confidence", "vision_candidate_identity",
    "vision_identity_confidence", "vision_classification_evidence",
    "vision_listing_kind", "vision_identity_basis",
    "verified_category", "verified_identity", "review_flags",
    "condition_usage_state", "condition_grade", "condition_confidence",
    "condition_functional_status", "condition_evidence",
    "condition_uncertainties", "condition_review_flags",
    "refactored_description",
)


def export_queue(source: Path, output: Path) -> int:
    data = json.loads(source.read_text(encoding="utf-8"))
    listings = data.get("listings")
    if not isinstance(listings, list):
        raise ValueError("Input must be fb_marketplace.py result JSON")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        for listing in listings:
            vision = listing.get("vision") or {}
            observations = vision.get("observations") or {}
            condition = listing.get("condition_assessment") or {}
            condition_result = condition.get("result") or {}
            image_file = listing.get("image_file") or ""
            writer.writerow({
                "listing_id": listing.get("listing_id", ""),
                "title": listing.get("title", ""),
                "description": listing.get("description") or "",
                "refactored_description": listing.get("refactored_description") or "",
                "asking_price": listing.get("asking_price", ""),
                "seller_location": listing.get("location", ""),
                "search_zip": listing.get("search_zip", data.get("requested_zip", "")),
                "proposed_item_id": (listing.get("identity") or {}).get("id") or "",
                "verified_item_id": "",
                "proposed_condition": (listing.get("condition") or {}).get("grade") or "",
                "verified_condition": "",
                "notes": "",
                "image_file": (str((source.parent / image_file).resolve())
                               if image_file else ""),
                "vision_model": vision.get("model") or "",
                "vision_primary_object": observations.get("primary_object") or "",
                "vision_visible_brand": observations.get("visible_brand") or "",
                "vision_visible_model": observations.get("visible_model") or "",
                "vision_wear_or_damage": " | ".join(observations.get("wear_or_damage") or []),
                "vision_uncertainties": " | ".join(observations.get("uncertainties") or []),
                "vision_category": observations.get("category") or "",
                "vision_listing_kind": observations.get("listing_kind") or "",
                "vision_item_type": observations.get("item_type") or "",
                "vision_category_confidence": observations.get("category_confidence") or "",
                "vision_candidate_identity": observations.get("candidate_identity") or "",
                "vision_identity_basis": observations.get("identity_basis") or "",
                "vision_identity_confidence": observations.get("identity_confidence") or "",
                "vision_classification_evidence": " | ".join(
                    observations.get("classification_evidence") or []),
                "verified_category": "",
                "verified_identity": "",
                "review_flags": " | ".join(vision.get("review_flags") or []),
                "condition_usage_state": condition_result.get("usage_state") or "",
                "condition_grade": condition_result.get("condition_grade") or "",
                "condition_confidence": condition_result.get("confidence") or "",
                "condition_functional_status": condition_result.get("functional_status") or "",
                "condition_evidence": " | ".join(condition_result.get("evidence") or []),
                "condition_uncertainties": " | ".join(
                    condition_result.get("uncertainties") or []),
                "condition_review_flags": " | ".join(condition.get("review_flags") or []),
                "verified_item_type": "",
                "verified_material": "",
            })
    return len(listings)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a human review sheet for model labels")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("runs/facebook_labels.csv"))
    args = parser.parse_args()
    count = export_queue(args.source, args.output)
    print(f"Wrote {count} rows to {args.output.resolve()}")
