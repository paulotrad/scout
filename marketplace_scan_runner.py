"""Run one local Marketplace scan and its AI review stages."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid


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
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    temp.replace(path)


def create_manifest(zip_code: object, radius_miles: object,
                    keywords: object = None, mode: str = "manual",
                    account_id: str | None = None) -> dict:
    zip_code, radius, keywords = validate_settings(
        zip_code, radius_miles, keywords)
    if mode not in {"manual", "scheduled"}:
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


def run_scan(scan_id: str) -> dict:
    """Execute trusted script paths with argument arrays and per-stage logs."""
    manifest = read_manifest(scan_id)
    scan_dir = manifest_path(scan_id).parent
    python = sys.executable

    def stage(label: str, script: str, arguments: list[str]) -> None:
        manifest["status"] = "running"
        manifest["stage"] = label
        save_manifest(manifest)
        with (scan_dir / "pipeline.log").open("a", encoding="utf-8") as log:
            log.write(f"\n[{utc_now()}] {label}\n")
            log.flush()
            completed = subprocess.run(
                [python, str(ROOT / script), *arguments],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                check=False, timeout=7200)
        if completed.returncode:
            raise RuntimeError(f"{label} failed; see pipeline.log")

    try:
        collection = scan_dir / "collection.json"
        nearby = scan_dir / "nearby.json"
        details = scan_dir / "details.json"
        vision = scan_dir / "vision.json"
        summarized = scan_dir / "summarized.json"
        conditioned = scan_dir / "conditioned.json"
        presented = scan_dir / "presented.json"
        scheduled = manifest.get("mode") == "scheduled"
        args = [
            "--zip-code", manifest["zip_code"],
            "--radius-miles", str(manifest["radius_miles"]),
            "--max-items", "20" if scheduled else "1000",
            "--scrolls", "5" if scheduled else "200",
            "--until-stale", "--headless", "--comp-searches", "0",
            "--output", str(collection),
        ]
        for keyword in manifest["keywords"]:
            args.extend(["--keyword", keyword])
        if any(word.casefold() == "free" for word in manifest["keywords"]):
            args.append("--include-free-search")
        stage("Collecting Marketplace cards", "fb_marketplace.py", args)
        captured = json.loads(collection.read_text(encoding="utf-8"))
        manifest["scanned_count"] = len(captured.get("listings") or [])
        manifest["radius_miles_applied"] = captured.get("radius_miles_applied")
        save_manifest(manifest)

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
            stage("Identifying items from photos and text", "marketplace_vision.py",
                  vision_args)
            vision_data = json.loads(vision.read_text(encoding="utf-8"))
            if all((row.get("vision") or {}).get("status") == "error"
                   for row in vision_data.get("listings") or []):
                raise RuntimeError("Could not run the image model; see pipeline.log")
            stage("Cleaning seller descriptions", "marketplace_description_summary.py", [
                str(vision), "--output", str(summarized), "--model", "qwen3:8b",
                "--limit", str(count), "--resume"])
            stage("Estimating new or used and condition", "marketplace_condition.py", [
                str(summarized), "--output", str(conditioned),
                "--model", "qwen3-vl:8b-instruct", "--limit", str(count),
                "--timeout", "300", "--resume"])
            stage("Writing item descriptions", "marketplace_listing_copy.py", [
                str(conditioned), "--output", str(presented)])
            stage("Preparing review sheet", "marketplace_label_queue.py", [
                str(presented), "--output", str(scan_dir / "review.csv")])
            manifest["result_file"] = str(presented.relative_to(ROOT))
        manifest["status"] = "complete"
        manifest["stage"] = "Complete"
        manifest["completed_at"] = utc_now()
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
