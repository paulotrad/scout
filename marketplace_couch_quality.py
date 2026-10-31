"""Apply the 8B couch quality pilot to new couch listings during scans."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import requests

from marketplace_atomic import write_json
from marketplace_couch_quality_pilot import MODEL, PROMPT_VERSION, assess
from marketplace_couch_quality_rules import apply_quality_guardrails
from marketplace_db import DEFAULT_DB, get_cached_analysis, store_result
from marketplace_value import furniture_signature
from marketplace_vision import listing_images, photo_hash_prefix


COUCH_FORMS = {"sofa", "loveseat", "sectional", "sofa_bed"}
COUCH_ITEM_TYPES = {"couch", "sofa", "loveseat", "sectional", "sofa_bed",
                    "sleeper_sofa", "reclining_sofa", "sectional_sofa", "futon"}


def is_couch_listing(listing: dict) -> bool:
    """Run only for a physical couch identified by the existing classifier."""
    vision = listing.get("vision") or {}
    observations = vision.get("observations") or {}
    if vision.get("status") == "skipped_price_range" or observations.get("listing_kind") != "product":
        return False
    item_type = re.sub(r"[^a-z0-9]+", "_", str(observations.get("item_type") or "").lower()).strip("_")
    if item_type in COUCH_ITEM_TYPES:
        return True
    for label in (observations.get("item_type"), listing.get("title")):
        form = furniture_signature(str(label or ""))
        if form and form["product_type"] in COUCH_FORMS:
            return True
    return False


def run(input_file: Path, output_file: Path, model: str = MODEL,
        timeout_seconds: int = 300, resume: bool = False,
        db_path: Path | None = DEFAULT_DB) -> dict:
    if timeout_seconds < 1:
        raise ValueError("Timeout must be positive")
    data = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("listings"), list):
        raise ValueError("Input must have a listings array")
    previous = {}
    if resume and output_file.is_file():
        old = json.loads(output_file.read_text(encoding="utf-8"))
        previous = {str(row.get("listing_id")): row.get("couch_quality") or {}
                    for row in old.get("listings") or [] if isinstance(row, dict)}
    output_file.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint() -> None:
        data["couch_quality_model"] = model
        data["couch_quality_prompt"] = PROMPT_VERSION
        data["couch_quality_eligible"] = sum(is_couch_listing(row) for row in data["listings"])
        data["couch_quality_processed"] = sum(
            (row.get("couch_quality") or {}).get("status") == "estimated"
            for row in data["listings"])
        data["couch_quality_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output_file, data)

    session = requests.Session()
    session.trust_env = False
    try:
        for index, listing in enumerate(data["listings"], 1):
            if not is_couch_listing(listing):
                continue
            listing_id = str(listing.get("listing_id") or "")
            title = str(listing.get("title") or "")
            description = str(listing.get("description") or "")
            photos, _ = listing_images(input_file, listing, db_path)
            image_bytes = photos if len(photos) > 1 else photos[0] if photos else None
            input_hash = hashlib.sha256(photo_hash_prefix(photos) + json.dumps(
                {"title": title, "description": description},
                ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            quality = previous.get(listing_id) or {}
            cache_source = "checkpoint"
            if not quality and db_path is not None and db_path.is_file() and listing_id:
                quality = get_cached_analysis(db_path, "facebook_marketplace", listing_id).get(
                    "couch_quality") or {}
                cache_source = "database"
            if (quality.get("model") == model and quality.get("prompt_version") == PROMPT_VERSION
                    and quality.get("input_hash") == input_hash
                    and quality.get("status") in {"estimated", "no_image"}):
                if quality["status"] == "estimated":
                    quality["guardrails"] = apply_quality_guardrails(
                        quality["assessment"], title, description)
                listing["couch_quality"] = quality
                print(f"{index}/{len(data['listings'])} {listing_id} cached {cache_source}", flush=True)
                checkpoint()
                continue
            if image_bytes is None:
                listing["couch_quality"] = {
                    "status": "no_image", "model": model,
                    "prompt_version": PROMPT_VERSION, "input_hash": input_hash,
                }
            else:
                try:
                    assessment = assess(session, image_bytes, title, description,
                                        timeout_seconds, model)
                    listing["couch_quality"] = {
                        "status": "estimated", "model": model,
                        "prompt_version": PROMPT_VERSION, "input_hash": input_hash,
                        "photo_count": len(photos),
                        "assessment": assessment,
                        "guardrails": apply_quality_guardrails(assessment, title, description),
                    }
                except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as error:
                    listing["couch_quality"] = {
                        "status": "error", "model": model,
                        "prompt_version": PROMPT_VERSION, "input_hash": input_hash,
                        "reason": str(error)[:200],
                    }
            print(f"{index}/{len(data['listings'])} {listing_id} "
                  f"{listing['couch_quality']['status']}", flush=True)
            checkpoint()
    finally:
        session.close()
    checkpoint()
    if db_path is not None:
        store_result(data, input_file.parent, db_path)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Review couch quality using Qwen VL 8B")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    result = run(args.input, args.output, args.model, args.timeout,
                 args.resume, args.db)
    print(json.dumps({"couch_quality_processed": result["couch_quality_processed"]}))


if __name__ == "__main__":
    main()
