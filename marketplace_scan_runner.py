"""Run one local Marketplace scan and its AI review stages."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from marketplace_db import DEFAULT_DB, mark_scan_seen, scope_is_seeded
from marketplace_fast_coverage import fast_scan_policy


ROOT = Path(__file__).resolve().parent
SCAN_ROOT = ROOT / "runs" / "scans"
SCAN_ID = re.compile(r"^scan-[a-z0-9-]{8,90}$")
ZIP = re.compile(r"^\d{5}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_settings(zip_code: object, radius_miles: object,
                      keywords: object = None) -> tuple[str, int, list[str]]:
    if not isinstance(zip_code, str) or not ZIP.fullmatch(zip_code):
        raise ValueError("Enter a five-digit US ZIP code")
    if isinstance(radius_miles, bool):
        raise ValueError("Radius must be between 1 and 100 miles")
    try:
        radius = int(radius_miles)
    except (TypeError, ValueError) as error:
        raise ValueError("Radius must be between 1 and 100 miles") from error
    if radius < 1 or radius > 100:
        raise ValueError("Radius must be between 1 and 100 miles")
    if keywords is None:
        keywords = ["free"]
    if not isinstance(keywords, list) or not keywords or len(keywords) > 25:
        raise ValueError("Enter one to 25 search keywords")
    clean = []
    for word in keywords:
        if not isinstance(word, str):
            raise ValueError("Keywords must be text")
        word = word.strip()
        if not 1 <= len(word) <= 80 or any(ord(char) < 32 for char in word):
            raise ValueError("Each keyword must be 1–80 printable characters")
        if word.casefold() not in {saved.casefold() for saved in clean}:
            clean.append(word)
    return zip_code, radius, clean


def manifest_path(scan_id: str) -> Path:
    if not SCAN_ID.fullmatch(scan_id):
        raise ValueError("Invalid scan ID")
    return SCAN_ROOT / scan_id / "manifest.json"


def read_manifest(scan_id: str) -> dict:
    path = manifest_path(scan_id)
    if not path.is_file():
        raise FileNotFoundError(scan_id)
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(manifest: dict) -> None:
    path = manifest_path(manifest["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = utc_now()
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    try:
        for attempt in range(15):
            try:
                temp.replace(path)
                return
            except PermissionError:
                if attempt == 14:
                    raise
                # Windows can briefly deny replacement while a page reads the
                # manifest. Keep the complete old file visible until retry.
                time.sleep(min(0.05 * (attempt + 1), 0.4))
    finally:
        temp.unlink(missing_ok=True)


def create_manifest(zip_code: object, radius_miles: object,
                    keywords: object = None, mode: str = "manual",
                    account_id: str | None = None) -> dict:
    zip_code, radius, keywords = validate_settings(
        zip_code, radius_miles, keywords)
    if mode not in {"manual", "scheduled", "incremental"}:
        raise ValueError("Invalid scan mode")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    scan_id = f"scan-{stamp}-{zip_code}-{radius}mi-{uuid.uuid4().hex[:6]}"
    manifest = {
        "id": scan_id, "zip_code": zip_code, "radius_miles": radius,
        "keywords": keywords, "mode": mode, "account_id": account_id,
        "status": "queued", "stage": "Waiting to start",
        "created_at": utc_now(), "result_file": None,
        "scanned_count": 0, "within_radius_count": 0,
    }
    save_manifest(manifest)
    return manifest


def seen_scope(manifest: dict) -> str | None:
    """Separate each person's keyword and filters when tracking seen cards."""
    account_id = manifest.get("account_id")
    watch = manifest.get("watch_item") or {}
    if not account_id or not watch:
        return None
    fields = [str(account_id), str(watch.get("term") or "").casefold(),
              str(watch.get("zip_code") or ""),
              int(watch.get("radius_miles") or 0),
              float(watch.get("min_price") or 0),
              float(watch.get("max_price") or 0)]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def seed_seen_scope(manifest: dict, scope: str) -> int:
    """Import completed scans made before per-search seen tracking existed."""
    db_path = ROOT / DEFAULT_DB
    if scope_is_seeded(db_path, scope):
        return 0
    previous_ids = []
    for path in SCAN_ROOT.glob("scan-*/manifest.json"):
        if path == manifest_path(manifest["id"]):
            continue
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("status") != "complete" or seen_scope(previous) != scope:
                continue
            nearby_path = path.parent / "nearby.json"
            priced_path = path.parent / "priced.json"
            if nearby_path.is_file():
                nearby = json.loads(nearby_path.read_text(encoding="utf-8"))
                priced = (json.loads(priced_path.read_text(encoding="utf-8"))
                          if priced_path.is_file() else None)
                previous_ids.extend(completed_listing_ids(nearby, priced))
            else:
                collection_path = path.parent / "collection.json"
                collection = json.loads(collection_path.read_text(encoding="utf-8"))
                previous_ids.extend(str(row["listing_id"]) for row in
                                    collection.get("listings") or []
                                    if row.get("listing_id"))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    mark_scan_seen(db_path, scope, previous_ids, seeded=True)
    return len(set(previous_ids))


def completed_listing_ids(filtered: dict, analyzed: dict | None) -> list[str]:
    """Only suppress listings with a finished analysis or a checked exclusion."""
    seen = {str(row["listing_id"]) for row in filtered.get("excluded_listings") or []
            if row.get("listing_id")}
    for row in (analyzed or {}).get("listings") or []:
        listing_id = row.get("listing_id")
        if not listing_id:
            continue
        vision = row.get("vision") or {}
        status = vision.get("status")
        if status in {"filtered_offer", "skipped_price_range"}:
            seen.add(str(listing_id))
        elif status == "observed":
            kind = (vision.get("observations") or {}).get("listing_kind")
            condition = (row.get("condition_assessment") or {}).get("status")
            if (kind in {"service", "promotion", "event_notice",
                         "mixed_or_unclear"} or
                    (kind == "product" and condition in
                     {"estimated", "not_applicable"})):
                seen.add(str(listing_id))
    return sorted(seen)


def run_scan(scan_id: str) -> dict:
    """Execute trusted script paths with argument arrays and per-stage logs."""
    manifest = read_manifest(scan_id)
    scan_dir = manifest_path(scan_id).parent
    python = sys.executable
    started = time.monotonic()

    def stage(label: str, script: str, arguments: list[str]) -> None:
        manifest["status"] = "running"
        manifest["stage"] = label
        save_manifest(manifest)
        with (scan_dir / "pipeline.log").open("a", encoding="utf-8") as log:
            log.write(f"\n[{utc_now()}] {label}\n")
            log.flush()
            attempts = 2 if script in {"fb_marketplace.py", "marketplace_details.py"} else 1
            for attempt in range(attempts):
                remaining = (1500 - (time.monotonic() - started)
                             if manifest.get("mode") == "incremental" else 7200)
                if remaining <= 0:
                    raise RuntimeError("Quick check exceeded 25 minutes; see pipeline.log")
                timeout = min(remaining, 7200)
                try:
                    completed = subprocess.run(
                        [python, str(ROOT / script), *arguments],
                        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                        check=False, timeout=timeout)
                    if completed.returncode == 0:
                        return
                    log.write(f"{label} exited with code {completed.returncode}.\n")
                except subprocess.TimeoutExpired:
                    log.write(f"{label} timed out after {int(timeout)} seconds.\n")
                log.flush()
                if attempt + 1 < attempts:
                    log.write("Retrying this stage once.\n")
                    log.flush()
                    time.sleep(5)
        raise RuntimeError(f"{label} failed; see pipeline.log")

    try:
        collection = scan_dir / "collection.json"
        nearby = scan_dir / "nearby.json"
        details = scan_dir / "details.json"
        vision = scan_dir / "vision.json"
        summarized = scan_dir / "summarized.json"
        conditioned = scan_dir / "conditioned.json"
        second_opinion = scan_dir / "second_opinion.json"
        couch_quality = scan_dir / "couch_quality.json"
        presented = scan_dir / "presented.json"
        priced = scan_dir / "priced.json"
        incremental = manifest.get("mode") == "incremental"
        scheduled = manifest.get("mode") in {"scheduled", "incremental"}
        fast_policy = fast_scan_policy(manifest) if incremental else None
        scope = seen_scope(manifest)
        new_only = incremental or (manifest.get("mode") == "scheduled" and bool(scope))
        args = [
            "--zip-code", manifest["zip_code"],
            "--radius-miles", str(manifest["radius_miles"]),
            "--max-items", str(fast_policy["max_items"]) if incremental
            else "120" if manifest.get("mode") == "scheduled" and new_only
            else "40" if scheduled else "1000",
            "--scrolls", str(fast_policy["scrolls"]) if incremental
            else "8" if scheduled else "200",
            "--until-stale", "--headless", "--comp-searches", "0",
            "--output", str(collection),
        ]
        for keyword in manifest["keywords"]:
            args.extend(["--keyword", keyword])
        if any(word.casefold() == "free" for word in manifest["keywords"]):
            args.append("--include-free-search")
        if incremental:
            if manifest.get("silent_pilot"):
                args.append("--id-only")
            if fast_policy["pilot_id"]:
                args.extend(["--coverage-depths", ",".join(
                    str(value) for value in fast_policy["depths"]),
                    "--coverage-pilot-id", fast_policy["pilot_id"]])
                manifest["coverage_pilot_id"] = fast_policy["pilot_id"]
                save_manifest(manifest)
        manifest["collection_new_only"] = new_only
        if new_only:
            args.append("--new-only")
        if manifest.get("mode") == "scheduled" and new_only:
            args.extend(["--max-new-items", "40"])
        if new_only and scope:
            seed_seen_scope(manifest, scope)
            args.extend(["--seen-key", scope])
        stage("Collecting Marketplace cards", "fb_marketplace.py", args)
        captured = json.loads(collection.read_text(encoding="utf-8"))
        manifest["scanned_count"] = len(captured.get("listings") or [])
        if new_only:
            manifest["cards_checked"] = captured.get("cards_checked", 0)
            manifest["known_cards_skipped"] = captured.get("known_cards_skipped", 0)
            manifest["new_cards_deferred"] = captured.get("new_cards_deferred", 0)
            manifest["rechecked_cards"] = captured.get("rechecked_cards", 0)
        if incremental:
            if captured.get("coverage_probe"):
                manifest["coverage_probe"] = captured["coverage_probe"]
        manifest["radius_miles_applied"] = captured.get("radius_miles_applied")
        manifest["search_center_offset_miles"] = captured.get("search_center_offset_miles")
        manifest["search_coverage_radius_miles"] = captured.get("search_coverage_radius_miles")
        save_manifest(manifest)
        if manifest.get("silent_pilot"):
            if scope:
                mark_scan_seen(ROOT / DEFAULT_DB, scope,
                               [str(row["listing_id"]) for row in
                                captured.get("listings") or []
                                if row.get("listing_id")],
                               cards_by_id={str(row["listing_id"]): row for row in
                                            captured.get("listings") or []
                                            if row.get("listing_id")})
            manifest["result_file"] = str(collection.relative_to(ROOT))
            manifest["status"] = "complete"
            manifest["stage"] = "Depth comparison complete"
            manifest.pop("error", None)
            manifest["completed_at"] = utc_now()
            save_manifest(manifest)
            return manifest

        stage("Checking the requested radius", "marketplace_radius.py", [
            str(collection), "--radius-miles", str(manifest["radius_miles"]),
            "--output", str(nearby)])
        filtered = json.loads(nearby.read_text(encoding="utf-8"))
        count = len(filtered.get("listings") or [])
        manifest["within_radius_count"] = count
        manifest["excluded_count"] = filtered.get("excluded_count", 0)
        manifest["result_file"] = str(nearby.relative_to(ROOT))
        save_manifest(manifest)
        if (manifest["scanned_count"] and not count and
                filtered.get("unresolved_count") == manifest["scanned_count"]):
            raise RuntimeError("Seller locations could not be checked; see pipeline.log")
        if count:
            stage("Reading seller descriptions", "marketplace_details.py", [
                str(nearby), "--output", str(details), "--limit", str(count)])
            detail_data = json.loads(details.read_text(encoding="utf-8"))
            if all(row.get("description_status") == "error"
                   for row in detail_data.get("listings") or []):
                raise RuntimeError("Could not read seller descriptions; see pipeline.log")
            vision_args = [
                str(details), "--output", str(vision), "--model", "qwen3-vl:8b-instruct",
                "--limit", str(count), "--timeout", "300", "--resume"]
            watch = manifest.get("watch_item") or {}
            if watch:
                vision_args.extend(["--min-price", str(watch["min_price"]),
                                    "--max-price", str(watch["max_price"])])
            # The vision worker writes atomic checkpoints after each card. Let
            # the scan page show identified items while later stages continue.
            manifest["result_file"] = str(vision.relative_to(ROOT))
            save_manifest(manifest)
            stage("Identifying items from photos and text", "marketplace_vision.py",
                  vision_args)
            vision_data = json.loads(vision.read_text(encoding="utf-8"))
            vision_rows = vision_data.get("listings") or []
            statuses = [(row.get("vision") or {}).get("status") for row in vision_rows]
            manifest["photo_analysis"] = {
                "analyzed": statuses.count("observed"),
                "seller_offers_filtered": statuses.count("filtered_offer"),
                "outside_price_range": statuses.count("skipped_price_range"),
                "errors": statuses.count("error"),
                "photos_missing": statuses.count("no_local_image"),
            }
            save_manifest(manifest)
            needs_photo_review = [status for status in statuses if status not in {
                "filtered_offer", "skipped_price_range"}]
            if needs_photo_review and "observed" not in needs_photo_review:
                raise RuntimeError(
                    "No eligible listing received a usable photo analysis; "
                    "see pipeline.log")
            stage("Cleaning seller descriptions", "marketplace_description_summary.py", [
                str(vision), "--output", str(summarized), "--model", "qwen3:8b",
                "--limit", str(count), "--resume"])
            manifest["result_file"] = str(summarized.relative_to(ROOT))
            save_manifest(manifest)
            stage("Estimating new or used and condition", "marketplace_condition.py", [
                str(summarized), "--output", str(conditioned),
                "--model", "qwen3-vl:8b-instruct", "--limit", str(count),
                "--timeout", "300", "--resume"])
            manifest["result_file"] = str(conditioned.relative_to(ROOT))
            save_manifest(manifest)
            review_input = conditioned
            review_model = os.environ.get("SCOUT_SECOND_OPINION_MODEL", "gemma3:12b").strip()
            if review_model and review_model.casefold() != "off":
                stage("Reviewing uncertain items locally", "marketplace_second_opinion.py", [
                    str(conditioned), "--output", str(second_opinion),
                    "--model", review_model, "--limit", "2", "--timeout", "120"])
                review_input = second_opinion
                manifest["result_file"] = str(second_opinion.relative_to(ROOT))
                save_manifest(manifest)
            stage("Checking couch quality evidence", "marketplace_couch_quality.py", [
                str(review_input), "--output", str(couch_quality),
                "--model", "qwen3-vl:8b-instruct", "--timeout", "300", "--resume"])
            manifest["result_file"] = str(couch_quality.relative_to(ROOT))
            save_manifest(manifest)
            stage("Writing item descriptions", "marketplace_listing_copy.py", [
                str(couch_quality), "--output", str(presented)])
            manifest["result_file"] = str(presented.relative_to(ROOT))
            save_manifest(manifest)
            stage("Applying reviewed local prices", "marketplace_pricing.py", [
                str(presented), "--output", str(priced)])
            stage("Preparing review sheet", "marketplace_label_queue.py", [
                str(priced), "--output", str(scan_dir / "review.csv")])
            manifest["result_file"] = str(priced.relative_to(ROOT))
        analyzed = json.loads(priced.read_text(encoding="utf-8")) if count else None
        completed_ids = completed_listing_ids(filtered, analyzed)
        completed_id_set = set(completed_ids)
        manifest["analysis_retry_pending_count"] = sum(
            bool(row.get("listing_id")) and
            str(row["listing_id"]) not in completed_id_set
            for row in filtered.get("listings") or [])
        manifest["status"] = "complete"
        manifest["stage"] = "Complete"
        manifest.pop("error", None)
        manifest["completed_at"] = utc_now()
        if scope:
            mark_scan_seen(ROOT / DEFAULT_DB, scope, completed_ids,
                           cards_by_id={str(row["listing_id"]): row for row in
                                        captured.get("listings") or []
                                        if row.get("listing_id")})
    except Exception as error:
        manifest["status"] = "failed"
        manifest["stage"] = "Scan failed"
        manifest["error"] = str(error)[:300]
    save_manifest(manifest)
    return manifest


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run a local Marketplace scan")
    parser.add_argument("--zip-code", default="21009")
    parser.add_argument("--radius-miles", type=int, default=15)
    parser.add_argument("--keyword", action="append")
    args = parser.parse_args()
    scan = create_manifest(args.zip_code, args.radius_miles,
                           args.keyword or ["free"])
    result = run_scan(scan["id"])
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "complete" else 1)
