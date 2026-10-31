"""Bounded, visible background research for sparse Marketplace price cohorts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading

from fb_marketplace import run as collect_marketplace
from marketplace_atomic import write_json
from marketplace_bundle import extract_bundle
from marketplace_completeness import incomplete_product
from marketplace_db import DEFAULT_DB
from marketplace_details import enrich_run
from marketplace_mower import WALK_BEHIND_TYPES, mower_peer, mower_variant
from marketplace_pricing import (GEOCODE_CACHE, _condition_for_pricing,
                                 run as apply_pricing)
from marketplace_radius import TownGeocoder, distance_miles, filter_result
from marketplace_reference_candidates import build as build_candidates
from marketplace_reference_candidates import screen_listing
from marketplace_value import (furniture_signature, general_reference_type,
                               scooter_form, scooter_model)
from marketplace_vision import seller_offer_override


ROOT = Path(__file__).resolve().parent
QUEUE_FILE = ROOT / "runs/price_research_queue.json"
WORK_ROOT = ROOT / "runs/price_research"
MIN_COMPARABLES = 3
MAX_CENTERS_PER_JOB = 6
MAX_CENTERS_PER_DAY = 12
RETRY_DELAY = timedelta(minutes=15)
GLOBAL_DELAY = timedelta(minutes=2)
_lock = threading.RLock()

# Search centers are separate from user ZIPs. Approximate coordinates only
# choose which centers to try; seller town distances are geocoded and checked.
CENTERS = {
    "02108": ("Boston", 42.36, -71.06), "10001": ("New York", 40.75, -73.99),
    "19103": ("Philadelphia", 39.95, -75.17), "21201": ("Baltimore", 39.29, -76.62),
    "20001": ("Washington", 38.91, -77.02), "23219": ("Richmond", 37.54, -77.44),
    "27601": ("Raleigh", 35.78, -78.64), "28202": ("Charlotte", 35.23, -80.84),
    "15222": ("Pittsburgh", 40.44, -80.00), "14202": ("Buffalo", 42.89, -78.88),
    "43215": ("Columbus", 39.96, -83.00), "44114": ("Cleveland", 41.50, -81.69),
    "48226": ("Detroit", 42.33, -83.05), "60601": ("Chicago", 41.89, -87.62),
    "46204": ("Indianapolis", 39.77, -86.16), "45202": ("Cincinnati", 39.10, -84.51),
    "37203": ("Nashville", 36.15, -86.79), "30303": ("Atlanta", 33.75, -84.39),
    "33130": ("Miami", 25.77, -80.20), "32801": ("Orlando", 28.54, -81.38),
    "33602": ("Tampa", 27.95, -82.46), "32202": ("Jacksonville", 30.33, -81.66),
    "35203": ("Birmingham", 33.52, -86.81), "75201": ("Dallas", 32.79, -96.80),
    "77002": ("Houston", 29.76, -95.36), "78701": ("Austin", 30.27, -97.74),
    "78205": ("San Antonio", 29.42, -98.49), "70112": ("New Orleans", 29.96, -90.07),
    "80202": ("Denver", 39.75, -105.00), "85004": ("Phoenix", 33.45, -112.07),
    "90012": ("Los Angeles", 34.06, -118.24), "92101": ("San Diego", 32.72, -117.16),
    "94103": ("San Francisco", 37.77, -122.41), "95814": ("Sacramento", 38.58, -121.49),
    "98101": ("Seattle", 47.61, -122.34), "97204": ("Portland", 45.52, -122.67),
    "55401": ("Minneapolis", 44.98, -93.27), "63101": ("St. Louis", 38.63, -90.20),
    "64106": ("Kansas City", 39.10, -94.58), "84101": ("Salt Lake City", 40.76, -111.90),
    "89101": ("Las Vegas", 36.17, -115.14), "73102": ("Oklahoma City", 35.47, -97.52),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: str | None) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def _load(path: Path = QUEUE_FILE) -> dict:
    if not path.is_file():
        return {"jobs": [], "last_attempt_at": None}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError("Invalid price research queue")
    return data


def _save(data: dict, path: Path = QUEUE_FILE) -> None:
    data["updated_at"] = _now().isoformat()
    write_json(path, data)


def _search_spec(listing: dict) -> tuple[str | None, dict | None, str]:
    if extract_bundle(listing):
        return None, None, "Bundle needs separate component price research"
    if incomplete_product(listing):
        return None, None, "Incomplete item needs parts-only price review"
    title = str(listing.get("title") or "")
    kind = general_reference_type(title)
    if kind == "electric_scooter" and (model := scooter_model(title)):
        form = scooter_form(title, str(listing.get("description") or ""))
        return model.replace("_", " ") + " electric scooter", {
            "family": "scooter", "model": model, "form": form}, ""
    furniture = furniture_signature(title)
    if furniture and furniture["product_type"] in {
            "recliner_chair", "sofa", "sofa_bed", "loveseat", "sectional",
            "dresser", "nightstand", "dining_table", "coffee_table", "end_table"}:
        grade, _ = _condition_for_pricing(listing)
        if (grade not in {"new", "like_new", "good", "fair"} or
                listing.get("description_status") != "visible" or
                not str(listing.get("description") or "").strip()):
            return None, furniture, "Furniture condition or seller details need review"
        form = furniture["product_type"]
        terms = {"recliner_chair": "recliner", "sofa": "couch",
                 "sofa_bed": "futon" if "futon" in title.casefold() else "sofa bed",
                 "loveseat": "loveseat", "sectional": "sectional couch",
                 "dresser": "dresser", "nightstand": "nightstand",
                 "dining_table": "dining table",
                 "coffee_table": "coffee table", "end_table": "end table"}
        materials = furniture["material_clues"]
        query = " ".join([*materials, terms[form]])
        return query, {"family": "furniture", "product_type": form,
                       "materials": materials, "condition_grade": grade}, ""
    if kind not in WALK_BEHIND_TYPES:
        return None, None, "Exact product variant needs review before broad searches"
    variant = mower_variant(title, str(listing.get("description") or ""), kind)
    if not variant or variant["power_source"] not in {"battery", "gas", "corded"}:
        return None, variant, "Mower power source needs review"
    if not variant["brand"] or not variant["deck_inches"]:
        return None, variant, "Mower brand or deck size needs review"
    if variant["power_source"] == "battery" and (
            not variant["voltage"] or variant["kit"] == "unknown"):
        return None, variant, "Battery voltage or included kit needs review"
    return f"{variant['brand']} {variant['deck_inches']} mower", variant, ""


def _safe_search_spec(listing: dict) -> tuple[str | None, dict | None, str]:
    query, variant, note = _search_spec(listing)
    if (listing.get("valuation") or {}).get("reason") == "item_identity_unconfirmed":
        return None, variant, "Confirm the item and variant before searching asking prices"
    return query, variant, note


def _job_key(listing: dict, variant: dict | None, zip_code: str) -> str:
    grade = (variant or {}).get("condition_grade") or _condition_for_pricing(listing)[0]
    basis = (json.dumps([zip_code, variant, grade], sort_keys=True)
             if variant else json.dumps([zip_code, listing.get("listing_id")]))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def enqueue_scan(scan_id: str, result_file: Path,
                 queue_file: Path = QUEUE_FILE) -> int:
    """Record every unresolved gap; auto-run only recognized exact variants."""
    data = json.loads(result_file.read_text(encoding="utf-8"))
    zip_code = str(data.get("requested_zip") or "")
    added = 0
    with _lock:
        queue = _load(queue_file)
        by_key = {job["key"]: job for job in queue["jobs"]}
        for listing in data.get("listings") or []:
            valuation = listing.get("valuation") or {}
            if (valuation.get("status") != "unvalued" or
                    valuation.get("reason") in {"not_single_item_offer", "pet_listing",
                                                "mixed_item_lot"} or
                    not listing.get("listing_id")):
                continue
            vision = listing.get("vision") or {}
            kind = (vision.get("observations") or {}).get("listing_kind")
            if (kind not in {None, "", "product"} or
                    "pet_listing" in (vision.get("review_flags") or []) or
                    seller_offer_override(listing.get("title") or "",
                                          listing.get("description") or "")):
                continue
            raw_count = valuation.get("comparable_count")
            count = max(0, raw_count) if type(raw_count) is int else 0
            if count >= MIN_COMPARABLES:
                continue
            query, variant, note = _safe_search_spec(listing)
            key = _job_key(listing, variant if query else None, zip_code)
            target = {"scan_id": scan_id, "listing_id": str(listing["listing_id"])}
            if job := by_key.get(key):
                if target not in job["targets"]:
                    job["targets"].append(target)
                if job["status"] != "complete":
                    job["price_points"] = min(job.get("price_points", count), count)
                continue
            job = {"id": "price-" + key, "key": key,
                   "title": str(listing.get("title") or "Untitled item")[:160],
                   "origin_zip": zip_code, "query": query, "variant": variant,
                   "price_points": count, "needed": MIN_COMPARABLES,
                   "status": "queued" if query else "needs_review",
                   "note": note, "targets": [target], "attempts": [],
                   "current_area": None, "next_attempt_at": _now().isoformat(),
                   "created_at": _now().isoformat()}
            queue["jobs"].append(job)
            by_key[key] = job
            added += 1
        if added or queue["jobs"]:
            _save(queue, queue_file)
    return added


def recover_interrupted(queue_file: Path = QUEUE_FILE) -> None:
    with _lock:
        queue = _load(queue_file)
        changed = False
        for job in queue["jobs"]:
            interrupted = (job["attempts"] and
                           job["attempts"][-1]["status"] == "running")
            if job["status"] == "running" or (job["status"] == "waiting" and interrupted):
                job["status"] = "waiting"
                job["note"] = "The previous research session stopped; it will retry"
                job["current_area"] = None
                job["next_attempt_at"] = _now().isoformat()
                if interrupted:
                    job["attempts"][-1].update({
                        "status": "failed", "error": "Research worker interrupted",
                        "completed_at": _now().isoformat()})
                changed = True
        if changed:
            _save(queue, queue_file)


def public_queue(queue_file: Path = QUEUE_FILE) -> dict:
    with _lock:
        queue = _load(queue_file)
    jobs = [{key: job.get(key) for key in (
        "id", "title", "origin_zip", "price_points", "needed", "status",
        "note", "current_area", "next_attempt_at", "attempts", "targets")}
        for job in queue["jobs"]]
    jobs.sort(key=lambda row: (row["status"] not in {"running", "queued", "waiting"},
                               row["price_points"], row["title"].casefold()))
    return {"jobs": jobs, "updated_at": queue.get("updated_at"),
            "policy": {"minimum_price_points": MIN_COMPARABLES,
                       "max_areas_per_job": MAX_CENTERS_PER_JOB,
                       "max_areas_per_day": MAX_CENTERS_PER_DAY}}


def _centers(job: dict, cache: dict) -> list[tuple[str, str]]:
    center = cache.get(job["origin_zip"])
    if not center:
        return []
    origin = (center["latitude"], center["longitude"])
    failed_counts = {zip_code: sum(row["zip_code"] == zip_code and
                                   row["status"] == "failed" for row in job["attempts"])
                     for zip_code in {row["zip_code"] for row in job["attempts"]}}
    attempted = {row["zip_code"] for row in job["attempts"]
                 if row["status"] == "complete" or failed_counts[row["zip_code"]] >= 2}
    if (job.get("variant") or {}).get("family") == "furniture":
        # Furniture pickup markets are local. Check the user's area once,
        # rather than cycling through overlapping distant metro searches.
        return ([] if job["origin_zip"] in attempted else
                [(job["origin_zip"], "Local area")])
    attempted_points = [(CENTERS[zip_code][1], CENTERS[zip_code][2])
                        for zip_code in attempted if zip_code in CENTERS]
    options = []
    for zip_code, (city, latitude, longitude) in CENTERS.items():
        miles = distance_miles(origin, (latitude, longitude))
        if (60 < miles <= 500 and zip_code not in attempted and
                all(distance_miles((latitude, longitude), prior) >= 90
                    for prior in attempted_points)):
            options.append((miles, zip_code, city))
    return [(zip_code, city) for _, zip_code, city in sorted(options)]


def claim_next(queue_file: Path = QUEUE_FILE,
               cache_file: Path = GEOCODE_CACHE,
               now: datetime | None = None) -> dict | None:
    """Reserve one different search area, subject to global daily limits."""
    now = now or _now()
    with _lock:
        queue = _load(queue_file)
        last = _time(queue.get("last_attempt_at"))
        if last and now - last < GLOBAL_DELAY:
            return None
        today = now.date()
        total_today = sum(_time(attempt.get("started_at")).date() == today
                          for job in queue["jobs"] for attempt in job["attempts"]
                          if _time(attempt.get("started_at")))
        if total_today >= MAX_CENTERS_PER_DAY:
            return None
        cache = (json.loads(cache_file.read_text(encoding="utf-8"))
                 if cache_file.is_file() else {})
        changed = False
        for job in queue["jobs"]:
            if job["status"] not in {"queued", "waiting"}:
                continue
            try:
                _, target_listing = _target_listing(job)
            except (OSError, ValueError, KeyError, TypeError):
                target_listing = None
            if target_listing and incomplete_product(target_listing):
                job["status"] = "needs_review"
                job["query"] = None
                job["note"] = "Incomplete item needs parts-only price review"
                changed = True
                continue
            due = _time(job.get("next_attempt_at"))
            if due and due > now:
                continue
            if not cache.get(job["origin_zip"]):
                job["status"] = "needs_review"
                job["note"] = "Search ZIP needs location verification"
                changed = True
                continue
            centers = _centers(job, cache)
            if not centers or len(job["attempts"]) >= MAX_CENTERS_PER_JOB:
                job["status"] = "exhausted"
                job["note"] = "No unused search area remains within 500 miles"
                changed = True
                continue
            zip_code, city = centers[0]
            job["status"] = "running"
            job["current_area"] = f"{city} {zip_code}"
            job["attempts"].append({"zip_code": zip_code, "area": city,
                                    "started_at": now.isoformat(), "status": "running"})
            queue["last_attempt_at"] = now.isoformat()
            _save(queue, queue_file)
            return json.loads(json.dumps(job))
        if changed:
            _save(queue, queue_file)
    return None


def _target_listing(job: dict) -> tuple[Path, dict]:
    for target in job["targets"]:
        path = ROOT / "runs/scans" / target["scan_id"] / "priced.json"
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        listing = next((row for row in data.get("listings") or []
                        if str(row.get("listing_id")) == target["listing_id"]), None)
        if listing:
            return path, listing
    raise FileNotFoundError("No saved target listing remains")


def _screen_center(job: dict, zip_code: str) -> tuple[int, int]:
    """Collect card titles, inspect likely peers, and approve only exact matches."""
    folder = WORK_ROOT / job["id"] / zip_code
    folder.mkdir(parents=True, exist_ok=True)
    collection = folder / "collection.json"
    if not collection.is_file():
        radius = 60 if job["variant"].get("family") == "furniture" else 100
        collect_marketplace(None, None,
            {"zip_code": zip_code, "radius_miles": radius,
             "keywords": [job["query"]]}, None, None, collection,
            max_items=60, scrolls=7, login_wait=20, headless=True,
            comp_searches=0, until_stale=True, id_only=True)
    data = json.loads(collection.read_text(encoding="utf-8"))
    variant = job["variant"]
    likely = []
    for row in data.get("listings") or []:
        title = str(row.get("title") or "")
        if variant.get("family") == "furniture":
            signature = furniture_signature(title)
            matches = bool(signature and
                signature["product_type"] == variant["product_type"] and
                set(variant["materials"]) <= set(signature["material_clues"]))
        elif variant.get("family") == "scooter":
            matches = scooter_model(title) == variant["model"]
        else:
            matches = bool(
                re.search(rf"\b{re.escape(variant['brand'])}\b", title, re.I) and
                re.search(rf"\b{variant['deck_inches']}\s*(?:inches?\b|in\b\.?|[\"”'])",
                          title, re.I))
        if matches:
            likely.append(row)
    subset = {**data, "listings": likely[:10]}
    write_json(folder / "likely.json", subset)
    if not subset["listings"]:
        return 0, 0
    radius = 60 if variant.get("family") == "furniture" else 100
    geocoder = TownGeocoder(GEOCODE_CACHE)
    try:
        nearby = filter_result(subset, geocoder, radius)
    finally:
        geocoder.close()
    write_json(folder / "nearby.json", nearby)
    if not nearby["listings"]:
        return 0, 0
    detail_path = folder / "details.json"
    saved_details = (json.loads(detail_path.read_text(encoding="utf-8"))
                     if detail_path.is_file() else None)
    details = (saved_details if saved_details and all(
               row.get("description_status") for row in saved_details["listings"])
               else enrich_run(folder / "nearby.json", detail_path,
                               resume=True, db_path=DEFAULT_DB))
    report = build_candidates(detail_path, folder / "candidates.json",
                              folder / "candidates.csv")
    _, target = _target_listing(job)
    target_type = general_reference_type(target.get("title", ""))
    target_furniture = (furniture_signature(target.get("title", ""))
                        if variant.get("family") == "furniture" else None)
    target_variant = (mower_variant(target.get("title", ""),
                                   target.get("description", ""), target_type)
                      if variant.get("family") != "scooter" else None)
    approved_ids = {str(item["listing_id"])
                    for path in (ROOT / "runs").glob("*_approved.json")
                    for item in json.loads(path.read_text(encoding="utf-8"))}
    approved = []
    target_ids = {str(target["listing_id"]) for target in job["targets"]}
    for listing in details["listings"]:
        screened = screen_listing(listing, details["captured_at"])
        kind = screened["reference_type"]
        if variant.get("family") == "furniture":
            signature = furniture_signature(listing.get("title", ""))
            compatible = bool(signature and target_furniture and
                signature["product_type"] == target_furniture["product_type"] ==
                variant["product_type"] and
                set(variant["materials"]) <= set(signature["material_clues"]))
        elif variant.get("family") == "scooter":
            compatible = (kind == "electric_scooter" and
                          scooter_model(listing.get("title", "")) == variant["model"] and
                          (variant["form"] != "seated" or
                           scooter_form(listing.get("title", ""),
                                        listing.get("description", "")) == "seated"))
        else:
            other = mower_variant(listing.get("title", ""),
                                  listing.get("description", ""), kind)
            compatible = bool(other and mower_peer(target_variant, other) and
                (kind == target_type or (
                    target_type == "push_mower" and kind == "lawn_mower" and
                    not re.search(r"\bself[ -]?propel", " ".join((
                        listing.get("title") or "", listing.get("description") or "")), re.I))))
        if (screened["review_status"] != "candidate" or not compatible or
                str(listing["listing_id"]) in approved_ids or
                str(listing["listing_id"]) in target_ids):
            continue
        approved.append({"source": "facebook_marketplace",
                         "listing_id": str(listing["listing_id"]),
                         "title": screened["title"],
                         "description": screened["description"],
                         "reference_type": kind,
                         "asking_price": screened["effective_ask"],
                         "condition": screened["condition"],
                         "search_zip": zip_code, "location": screened["location"],
                         "source_url": screened["url"],
                         "observed_at": screened["observed_at"],
                         "reference_radius_miles": radius,
                         "price_reviewed": True,
                         "review_basis": ("automatic_same_furniture_form_and_material_screen"
                                          if variant.get("family") == "furniture" else
                                          "automatic_exact_variant_screen"),
                         "price_basis": "seller_asking_price_not_completed_sale"})
    if approved:
        output = ROOT / "runs" / f"{job['id']}_{zip_code}_approved.json"
        write_json(output, approved)
    return report["candidate_count"], len(approved)


def finish_attempt(job_id: str, *, candidate_count: int = 0,
                   approved_count: int = 0, price_points: int | None = None,
                   error: str | None = None,
                   queue_file: Path = QUEUE_FILE) -> None:
    with _lock:
        queue = _load(queue_file)
        job = next(item for item in queue["jobs"] if item["id"] == job_id)
        attempt = job["attempts"][-1]
        attempt.update({"status": "failed" if error else "complete",
                        "candidate_count": candidate_count,
                        "approved_count": approved_count,
                        "completed_at": _now().isoformat()})
        job["current_area"] = None
        if price_points is not None:
            job["price_points"] = price_points
        if error:
            attempt["error"] = error[:200]
            job["note"] = ("Local area check failed; retry scheduled"
                           if (job.get("variant") or {}).get("family") == "furniture"
                           else "Area check failed; another area will be tried")
        elif job["price_points"] >= MIN_COMPARABLES:
            attempt.pop("error", None)
            job["status"] = "complete"
            job["note"] = "Enough compatible asking prices found"
        else:
            attempt.pop("error", None)
            job["note"] = (f"{approved_count} model-matched asking prices saved in "
                           f"{attempt['area']}; {MIN_COMPARABLES - job['price_points']} "
                           "still needed for this item's condition")
        if job["status"] != "complete":
            cache = (json.loads(GEOCODE_CACHE.read_text(encoding="utf-8"))
                     if GEOCODE_CACHE.is_file() else {})
            remaining = _centers(job, cache)
            job["status"] = "waiting" if remaining and len(job["attempts"]) < MAX_CENTERS_PER_JOB else "exhausted"
            if job["status"] == "exhausted":
                job["note"] = ("Local area check could not be completed; "
                               "a person should review this category"
                               if error and (job.get("variant") or {}).get(
                                   "family") == "furniture" else
                               f"{job['price_points']} of {MIN_COMPARABLES} compatible "
                               "asking prices found; no more area checks are scheduled")
            job["next_attempt_at"] = (_now() + RETRY_DELAY).isoformat()
        _save(queue, queue_file)


def run_claimed(job: dict, queue_file: Path = QUEUE_FILE) -> None:
    attempt = job["attempts"][-1]
    try:
        candidate_count, approved_count = _screen_center(job, attempt["zip_code"])
        counts = []
        for target in job["targets"]:
            path = ROOT / "runs/scans" / target["scan_id"] / "priced.json"
            if not path.is_file():
                continue
            updated = apply_pricing(path, path, reference_time=_now())
            listing = next((row for row in updated.get("listings") or []
                            if str(row.get("listing_id")) == target["listing_id"]), None)
            if listing:
                counts.append(int((listing.get("valuation") or {}).get(
                    "comparable_count") or 0))
        finish_attempt(job["id"], candidate_count=candidate_count,
                       approved_count=approved_count,
                       price_points=min(counts) if counts else None,
                       queue_file=queue_file)
        if approved_count:
            refresh_coverage(queue_file=queue_file)
    except Exception as error:
        finish_attempt(job["id"], error=f"{type(error).__name__}: {error}",
                       queue_file=queue_file)


def refresh_coverage(queue_file: Path = QUEUE_FILE,
                     *, reprice: bool = True) -> int:
    """Update old targets when the approved pool changes or Scout restarts."""
    with _lock:
        queue = _load(queue_file)
        targets = {target["scan_id"] for job in queue["jobs"]
                   for target in job["targets"]}
    by_scan = {}
    for scan_id in targets:
        path = ROOT / "runs/scans" / scan_id / "priced.json"
        if not path.is_file():
            continue
        try:
            data = (apply_pricing(path, path, reference_time=_now()) if reprice
                    else json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            continue
        by_scan[scan_id] = {str(row.get("listing_id")): row.get("valuation") or {}
                            for row in data.get("listings") or []}
    changed = 0
    with _lock:
        queue = _load(queue_file)
        for job in queue["jobs"]:
            counts = []
            for target in job["targets"]:
                valuation = by_scan.get(target["scan_id"], {}).get(target["listing_id"])
                if valuation is not None:
                    counts.append(int(valuation.get("comparable_count") or 0))
            if not counts:
                continue
            points = min(counts)
            if points != job["price_points"]:
                job["price_points"] = points
                changed += 1
            if (points < MIN_COMPARABLES and job["status"] in {"waiting", "exhausted"} and
                    job["attempts"] and job["attempts"][-1]["status"] == "complete"):
                latest = job["attempts"][-1]
                note = (f"{points} of {MIN_COMPARABLES} compatible asking prices "
                        "found; no more area checks are scheduled"
                        if job["status"] == "exhausted" else
                        f"{latest.get('approved_count', 0)} model-matched asking "
                        f"prices saved in {latest['area']}; {MIN_COMPARABLES - points} "
                        "still needed for this item's condition")
                if job["note"] != note:
                    job["note"] = note
                    changed += 1
            if points >= MIN_COMPARABLES and job["status"] != "complete":
                job["status"] = "complete"
                job["current_area"] = None
                job["note"] = "Enough compatible asking prices found"
                changed += 1
            elif (points < MIN_COMPARABLES and job["status"] == "complete" and
                  job.get("query") and len(job["attempts"]) < MAX_CENTERS_PER_JOB):
                job["status"] = "waiting"
                job["note"] = "Price references aged out; more research is queued"
                job["next_attempt_at"] = _now().isoformat()
                changed += 1
        if changed:
            _save(queue, queue_file)
    return changed


def refresh_eligibility(queue_file: Path = QUEUE_FILE) -> int:
    """Promote saved gaps when a conservative matching rule becomes available."""
    with _lock:
        queue = _load(queue_file)
    updates = {}
    for job in queue["jobs"]:
        if job["status"] != "needs_review":
            continue
        try:
            _, listing = _target_listing(job)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        query, variant, note = _safe_search_spec(listing)
        if query:
            updates[job["id"]] = (query, variant, note,
                                  _job_key(listing, variant, job["origin_zip"]))
    if not updates:
        return 0
    with _lock:
        queue = _load(queue_file)
        merged = set()
        for job in queue["jobs"]:
            if job["id"] not in updates or job["status"] != "needs_review":
                continue
            query, variant, note, key = updates[job["id"]]
            peer = next((other for other in queue["jobs"]
                         if other is not job and other["key"] == key), None)
            if peer:
                for target in job["targets"]:
                    if target not in peer["targets"]:
                        peer["targets"].append(target)
                merged.add(job["id"])
                continue
            job["query"], job["variant"], job["note"] = query, variant, note
            job["key"] = key
            job["id"] = "price-" + key
            job["status"] = "queued"
            job["next_attempt_at"] = _now().isoformat()
        if merged:
            queue["jobs"] = [job for job in queue["jobs"] if job["id"] not in merged]
        _save(queue, queue_file)
    return len(updates)
