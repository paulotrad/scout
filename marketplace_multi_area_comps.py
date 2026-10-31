"""Finish a multi-ZIP reference scan using verified seller descriptions."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from marketplace_db import DEFAULT_DB, store_result
from marketplace_reference_candidates import build, approved_pricing_comparables


ROOT = Path(__file__).resolve().parent
DETAIL_FIELDS = ("description", "seller_condition_label", "description_status",
                 "description_truncated", "detail_captured_at")


def finalize(directory: Path, approved_file: Path,
             db_path: Path | None = DEFAULT_DB) -> dict:
    descriptions = json.loads((directory / "all_details.json").read_text(encoding="utf-8"))
    by_id = {str(row["listing_id"]): row for row in descriptions["listings"]}
    if len(by_id) != len(descriptions["listings"]):
        raise ValueError("Detail batch contains duplicate listing IDs")
    earlier = {str(row["listing_id"])
               for path in (ROOT / "runs").glob("*_approved.json")
               if path.resolve() != approved_file.resolve()
               for row in json.loads(path.read_text(encoding="utf-8"))}
    selected = set(earlier)
    approved = []
    centers = []
    for near_file in sorted(directory.glob("*/nearby.json")):
        source = json.loads(near_file.read_text(encoding="utf-8"))
        listings = []
        for item in source["listings"]:
            detail = by_id.get(str(item["listing_id"]))
            if detail is None or not detail.get("description_status"):
                raise ValueError(f"Description pass unfinished for {item['listing_id']}")
            listings.append({**item, **{key: detail.get(key) for key in DETAIL_FIELDS}})
        source["listings"] = listings
        detail_file = near_file.with_name("details.json")
        detail_file.write_text(json.dumps(source, indent=2, ensure_ascii=False),
                               encoding="utf-8")
        if db_path is not None:
            store_result(source, near_file.parent, db_path)
        report = build(detail_file, near_file.with_name("candidates.json"),
                       near_file.with_name("candidates.csv"))
        chosen = approved_pricing_comparables(report, selected)
        selected.update(str(row["listing_id"]) for row in chosen)
        approved.extend(chosen)
        centers.append({"zip_code": source["requested_zip"],
                        "radius_miles": source["radius_miles_target"],
                        "nearby_cards": len(listings),
                        "clean_candidates": report["candidate_count"],
                        "new_references": len(chosen),
                        "needs_review": report["needs_review_count"]})
    approved_file.parent.mkdir(parents=True, exist_ok=True)
    approved_file.write_text(json.dumps(approved, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    summary = {"centers": centers, "new_reference_count": len(approved),
               "existing_reference_count": len(earlier),
               "price_basis": "seller_asking_price_not_completed_sale",
               "by_type": dict(sorted(Counter(row["reference_type"]
                                              for row in approved).items())),
               "approved_file": str(approved_file.resolve())}
    (directory / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Finalize multiple local price scans")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--approved", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    print(json.dumps(finalize(args.directory, args.approved, args.db), indent=2))


if __name__ == "__main__":
    main()
