"""Local website and API for Marketplace scans and saved AI results."""

from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from datetime import datetime, timedelta, timezone
import mimetypes
from pathlib import Path
import re
from statistics import median
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from marketplace_alerts import (
    DEMO_ACCOUNT, account_id_for_search, claim_slot, create_search,
    delete_search, due_fast_slot, due_slot, finish_slot, get_account, get_search,
    hide_all_inbox_alerts,
    list_account_profiles, list_admin_emails, list_alerts, list_searches,
    mark_read, open_account, is_admin_email, pending_slots, record_scan_matches,
    remove_nonproduct_alerts,
    release_slot, save_account,
    set_alert_triage,
    scheduled_scan_owners,
    watch_slot_key,
)
from marketplace_db import DEFAULT_DB, get_image, get_images
from marketplace_couch_comps import prepare_candidate
from marketplace_couch_quality_rules import apply_quality_guardrails
from marketplace_condition import condition_guardrails
from marketplace_coverage import mower_coverage
from marketplace_pricing import APPROVED_COUCH_POOL, _recent_price, _with_saved_descriptions
from marketplace_value import _percentile, classify_asking_price, estimate_value
from marketplace_email import (
    alert_item_summary, deliver_pending_summaries, email_status, finish_email_scan,
    prepare_email_batch,
)
from marketplace_feedback import (correction_count, corrections_to_review,
                                  feedback_for_listings, feedback_metrics,
                                  save_feedback)
from marketplace_health import (alert_events as health_alert_events,
                                benchmark_csv, feedback_events as health_feedback_events,
                                health_report, load_benchmark,
                                scan_summaries)
from marketplace_fast_coverage import (finish_pilot_if_due, load_pilot,
                                       public_pilot_status)
from marketplace_access import AccessDenied, verify_access_token
from marketplace_listing_copy import (display_title, mixed_item_lot,
                                      mixed_lot_summary, unidentified_lot)
from marketplace_vision import (live_animal_listing, seller_named_ski_machine,
                                seller_offer_override)
from marketplace_label_queue import export_queue
from marketplace_price import clear_free_offer, display_price
from marketplace_used_sources import source_plan as used_source_plan
from marketplace_craigslist import (SearchLimitReached, SearchUnavailable,
                                    search_craigslist)
from marketplace_price_research import (claim_next as claim_price_research,
                                        enqueue_scan as enqueue_price_research,
                                        public_queue as price_research_queue,
                                        recover_interrupted as recover_price_research,
                                        refresh_coverage as refresh_price_research,
                                        refresh_eligibility as refresh_price_research_eligibility,
                                        run_claimed as run_price_research)
from marketplace_scan_runner import (
    ROOT, SCAN_ROOT, create_manifest, read_manifest, run_scan, save_manifest,
)


WEB = ROOT / "marketplace_site"
PUBLIC_PREFIX = "/notify"
LEGACY_PUBLIC_PREFIX = "/Facebooknotify"
PUBLIC_HOST = os.environ.get("SCOUT_PUBLIC_HOST", "").strip().lower()
PUBLIC_WRITES = os.environ.get("SCOUT_PUBLIC_WRITES", "") == "1"
CF_TEAM_DOMAIN = os.environ.get("SCOUT_CF_TEAM_DOMAIN", "").strip().lower()
CF_AUD = os.environ.get("SCOUT_CF_AUD", "").strip()
IMAGE_ROUTE = re.compile(r"^/api/scans/(scan-[a-z0-9-]+)/images/(\d+)(?:/([0-3]))?$")
COUCH_IMAGE_ROUTE = re.compile(r"^/api/comparables/couches/images/(\d+)$")
GENERAL_IMAGE_ROUTE = re.compile(r"^/api/comparables/general/images/(\d+)$")
COUCH_DETAILS = ROOT / "runs/couch_comparables_21009_30mi_details.json"
COUCH_SUMMARY = ROOT / "runs/couch_comparables_21009_30mi_summary.json"
COUCH_PRICE_REVIEW = ROOT / "runs/couch_comparables_21009_30mi_price_review.json"
COUCH_QUALITY_PILOT = ROOT / "runs/couch_quality_8b_pilot.json"
GENERAL_CANDIDATES = ROOT / "runs/general_comps_21009_60mi_20261026/candidates.json"
APPROVED_GENERAL_POOL = ROOT / "runs/general_comps_21009_60mi_20261026_approved.json"
SCAN_ROUTE = re.compile(r"^/api/scans/(scan-[a-z0-9-]+)$")
USED_CANDIDATES_ROUTE = re.compile(
    r"^/api/scans/(scan-[a-z0-9-]+)/used-candidates/(\d+)$")
SCAN_FEEDBACK_ROUTE = re.compile(r"^/api/scans/(scan-[a-z0-9-]+)/feedback$")
ITEM_FEEDBACK_ROUTE = re.compile(
    r"^/api/scans/(scan-[a-z0-9-]+)/items/(\d+)/feedback$")
PAGE_ROUTE = re.compile(r"^/scan/(scan-[a-z0-9-]+)$")
SEARCH_ROUTE = re.compile(r"^/api/searches/(\d+)$")
SEARCH_SCAN_ROUTE = re.compile(r"^/api/searches/(\d+)/scan$")
ALERT_READ_ROUTE = re.compile(r"^/api/alerts/(\d+)/read$")
ALERT_TRIAGE_ROUTE = re.compile(r"^/api/alerts/(\d+)/triage$")
MAX_BODY = 16_384
active_lock = threading.Lock()
active_scan_id: str | None = None
price_research_active = False


def route_path(path: str) -> tuple[str, str]:
    """Map the optional Cloudflare path prefix to Scout's local routes."""
    for prefix in (PUBLIC_PREFIX, LEGACY_PUBLIC_PREFIX):
        if path == prefix:
            return "/", prefix
        if path.startswith(prefix + "/"):
            return path[len(prefix):], prefix
    return path, ""


def couch_comparables() -> dict:
    """Return the saved asking-price review without local file paths."""
    details = json.loads(COUCH_DETAILS.read_text(encoding="utf-8"))
    summary = json.loads(COUCH_SUMMARY.read_text(encoding="utf-8"))
    review = (json.loads(COUCH_PRICE_REVIEW.read_text(encoding="utf-8"))
              if COUCH_PRICE_REVIEW.is_file() else {})
    reviewed_ids = set(review.get("price_reviewed_listing_ids") or [])
    price_pool = (json.loads(APPROVED_COUCH_POOL.read_text(encoding="utf-8"))
                  if APPROVED_COUCH_POOL.is_file() else [])
    price_pool = [row for row in price_pool
                  if _recent_price(row, datetime.now(timezone.utc))]
    pilot = (json.loads(COUCH_QUALITY_PILOT.read_text(encoding="utf-8"))
             if COUCH_QUALITY_PILOT.is_file() else {})
    pilot_by_id = {str(row.get("listing_id")): row.get("assessment")
                   for row in pilot.get("results", [])
                   if row.get("status") == "ok" and isinstance(row.get("assessment"), dict)}
    pool_ids = {str(row.get("listing_id")) for row in price_pool}
    summary["price_reviewed_count"] = len(reviewed_ids)
    summary["estimation_pool_count"] = len(pool_ids)
    summary["price_window_days"] = 365
    summary["current_price_reference"] = bool(price_pool)
    summary["quality_pilot_count"] = len(pilot_by_id)
    summary["quality_pilot_model"] = pilot.get("model") or ""
    if not price_pool:
        summary["by_subtype"] = {}
    observed_at = str(details.get("captured_at") or "")
    listings = []
    for row in details["listings"]:
        candidate = prepare_candidate(row, observed_at)
        candidate["description"] = str(row.get("description") or "")[:1500]
        candidate["seller_condition_label"] = str(row.get("seller_condition_label") or "")
        candidate["image_url"] = f"/api/comparables/couches/images/{candidate['listing_id']}"
        candidate["price_reviewed"] = candidate["listing_id"] in reviewed_ids
        candidate["in_estimation_pool"] = candidate["listing_id"] in pool_ids
        candidate["quality_pilot"] = pilot_by_id.get(candidate["listing_id"])
        candidate["quality_guardrails"] = (
            apply_quality_guardrails(candidate["quality_pilot"], candidate["title"], candidate["description"])
            if candidate["quality_pilot"] else None
        )
        if candidate["in_estimation_pool"] and price_pool:
            target = {
                "listing_id": candidate["listing_id"], "title": candidate["title"],
                "search_zip": str(details.get("requested_zip") or ""),
                "condition": {"grade": candidate["condition"]},
                "identity": {"id": None},
            }
            value = estimate_value(target, price_pool)
            candidate["price_category"] = classify_asking_price(
                candidate["effective_ask"], value.get("asking_price_sample"))
        listings.append(candidate)
    return {"summary": summary, "listings": listings}


def general_comparable_reports() -> list[dict]:
    """Read price-review runs, including the newest multi-area mower batch."""
    paths = [GENERAL_CANDIDATES,
             ROOT / "runs/mower_comps_21009_60mi_20261026/candidates.json",
             *sorted((ROOT / "runs/mower_comps_multi_20261026").glob(
                  "*/candidates.json")),
              *sorted((ROOT / "runs").glob(
                  "reference_expansion_*/*/candidates.json")),
              *sorted((ROOT / "runs/price_research").glob(
                  "price-*/*/candidates.json"))]
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in paths if path.is_file()]


def general_comparables() -> dict:
    """Show distinct screened asking prices across all scanned ZIP areas."""
    reports = general_comparable_reports()
    if not reports:
        raise FileNotFoundError("No price-review reports exist")
    approved_rows = [row for path in (ROOT / "runs").glob("*_approved.json")
                     for row in json.loads(path.read_text(encoding="utf-8"))]
    approved_ids = {str(row["listing_id"]) for row in approved_rows}
    coverage = mower_coverage(_with_saved_descriptions(
        approved_rows, ROOT / DEFAULT_DB))
    by_id = {}
    for report in reports:
        for row in report.get("listings") or []:
            listing_id = str(row.get("listing_id") or "")
            if listing_id:
                by_id[listing_id] = {**row,
                                     "search_zip": str(report.get("requested_zip") or "")}
    listings = [{**row,
                 "image_url": f"/api/comparables/general/images/{listing_id}",
                 "in_estimate_pool": listing_id in approved_ids}
                for listing_id, row in by_id.items()]
    by_type = {}
    for reference_type in sorted({row["reference_type"] for row in listings
                                  if row["in_estimate_pool"]}):
        prices = sorted(float(row["effective_ask"]) for row in listings
                        if row["reference_type"] == reference_type and
                        row["in_estimate_pool"] and
                        isinstance(row.get("effective_ask"), (int, float)))
        if prices:
            by_type[reference_type] = {
                "candidate_count": len(prices),
                "median_asking_price": round(median(prices), 2),
                "middle_half_asking_range": [round(_percentile(prices, 0.25), 2),
                                               round(_percentile(prices, 0.75), 2)],
            }
    return {"summary": {
                "requested_zips": sorted({str(report.get("requested_zip") or "")
                                          for report in reports if report.get("requested_zip")}),
                "radius_miles": max((int(report.get("radius_miles") or 0)
                                     for report in reports), default=60),
                "observed_at": max(str(report.get("observed_at") or "")
                                   for report in reports),
                "approved_count": sum(row["in_estimate_pool"] for row in listings),
                "candidate_count": sum(row["review_status"] == "candidate"
                                       for row in listings),
                "needs_review_count": sum(row["review_status"] == "review" and
                                          not row["in_estimate_pool"]
                                          for row in listings),
                "by_type": by_type,
                "mower_coverage": coverage},
            "listings": listings}


def scan_manifests() -> list[dict]:
    SCAN_ROOT.mkdir(parents=True, exist_ok=True)
    records = []
    for path in SCAN_ROOT.glob("scan-*/manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            progress = scan_stage_progress(manifest)
            if progress:
                manifest["stage_progress"] = progress
            if manifest.get("status") in {"complete", "running"}:
                try:
                    preview = public_scan(manifest)
                    manifest["result_count"] = preview["result_count"]
                    if manifest.get("status") == "running":
                        manifest["early_items"] = [
                            {"id": item["id"],
                             "title": item["display_title"]}
                            for item in (preview.get("listings") or [])[:3]]
                except (OSError, ValueError, KeyError, TypeError):
                    manifest["result_count"] = None
            records.append(manifest)
        except (OSError, ValueError):
            continue
    return sorted(records, key=lambda row: row.get("created_at") or "",
                  reverse=True)


def scan_stage_progress(manifest: dict) -> dict | None:
    """Report model work already checkpointed while a scan is running."""
    if manifest.get("status") != "running":
        return None
    stages = {
        "Identifying items from photos and text":
            ("vision.json", "vision", "Photo identification"),
        "Estimating new or used and condition":
            ("conditioned.json", "condition_assessment", "Condition review"),
        "Reviewing uncertain items locally":
            ("second_opinion.json", "second_opinion", "Second opinion"),
        "Checking couch quality evidence":
            ("couch_quality.json", "couch_quality", "Couch quality review"),
    }
    stage = stages.get(manifest.get("stage"))
    if stage is None:
        return None
    scan_id = str(manifest.get("id") or "")
    if not re.fullmatch(r"scan-[a-z0-9-]+", scan_id):
        return None
    filename, field, label = stage
    path = SCAN_ROOT / scan_id / filename
    if not path.is_file():
        return None
    try:
        stage_data = json.loads(path.read_text(encoding="utf-8"))
        rows = stage_data.get("listings") or []
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(rows, list):
        return None
    processed = sum(isinstance(row, dict) and
                    bool((row.get(field) or {}).get("status")) for row in rows)
    total = (int(stage_data.get("couch_quality_eligible") or 0)
             if field == "couch_quality" else
             int(stage_data.get("second_opinion_selected") or 0)
             if field == "second_opinion" else
             int(manifest.get("within_radius_count") or len(rows)))
    if total < 1:
        return None
    return {"label": label, "processed": min(processed, total),
            "total": total}


def admin_overview(profiles: list[dict] | None = None,
                   manifests: list[dict] | None = None,
                   owners: dict[str, str] | None = None) -> dict:
    """Show owned scans and older scans that only match a profile's area."""
    profiles = list_account_profiles() if profiles is None else profiles
    manifests = scan_manifests() if manifests is None else manifests
    owners = scheduled_scan_owners() if owners is None else owners
    accounts = []
    shown_scan_ids = set()
    for profile in profiles:
        scans = []
        for manifest in manifests:
            owner = manifest.get("account_id") or owners.get(manifest.get("id"))
            if owner == profile["id"]:
                relation = "account"
            elif owner is None and profile["id"] == DEMO_ACCOUNT and (
                                    manifest.get("zip_code") == profile["zip_code"] and
                                    manifest.get("radius_miles") == profile["radius_miles"]):
                relation = "area_match"
            else:
                continue
            scans.append({**manifest, "relationship": relation})
            shown_scan_ids.add(manifest["id"])
        accounts.append({**profile, "scans": scans, "scan_count": len(scans),
                         "daily_scan_times": ["07:00", "12:00"] if
                         profile["plan"] == "plus_preview" else ["07:00"]})
    return {
        "accounts": accounts,
        "profile_count": len(accounts),
        "paid_subscriber_count": 0,
        "preview_count": sum(account["plan"] == "plus_preview" for account in accounts),
        "unassigned_scans": [scan for scan in manifests
                             if scan.get("id") not in shown_scan_ids],
        "total_scan_count": len(manifests),
    }


def quality_overview(manifests: list[dict] | None = None,
                     db_path: Path | None = None) -> dict:
    """A bounded, deduplicated view of recent uncertain results and corrections."""
    manifests = scan_manifests() if manifests is None else manifests
    recent = sorted((scan for scan in manifests if scan.get("status") == "complete"),
                    key=lambda scan: scan.get("created_at") or "", reverse=True)[:10]
    items = {}
    for scan in recent:
        try:
            for item in public_scan(scan)["listings"]:
                items.setdefault(item["id"], {**item, "scan_id": scan["id"]})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    rows = list(items.values())
    feedback_db = db_path or ROOT / DEFAULT_DB
    corrections = corrections_to_review(50, feedback_db)
    for correction in corrections[:12]:
        item = items.get(correction["listing_id"])
        if item is None:
            try:
                item = next((candidate for candidate in
                             public_scan(read_manifest(correction["scan_id"]))["listings"]
                             if candidate["id"] == correction["listing_id"]), None)
            except (OSError, ValueError, KeyError, TypeError):
                item = None
        correction["title"] = (item or {}).get("display_title") or "Marketplace item"
    review = [item for item in rows if not item.get("pet_listing") and
              (item.get("condition_grade") in {"", "unknown"}
              or (item.get("valuation") or {}).get("status") == "unvalued"
              or set(item.get("review_flags") or []).intersection({
                  "item_identity_conflict", "second_opinion_disagreement"}))]
    review.sort(key=lambda item: (
        0 if "item_identity_conflict" in (item.get("review_flags") or []) else
        1 if item.get("condition_grade") in {"", "unknown"} else 2,
        item.get("display_title") or ""))
    return {"scan_count": len(recent), "item_count": len(rows),
            "unknown_condition_count": sum(not item.get("pet_listing") and
                                           item.get("condition_grade") in {"", "unknown"}
                                           for item in rows),
            "unpriced_count": sum(not item.get("pet_listing") and
                                  (item.get("valuation") or {}).get("status") == "unvalued"
                                  for item in rows),
            "correction_count": correction_count(feedback_db),
            "feedback_metrics": feedback_metrics(feedback_db),
            "corrections": corrections[:12],
            "review_items": [{"scan_id": item["scan_id"], "id": item["id"],
                              "title": item.get("display_title") or item.get("title") or "Item",
                              "item_confidence": item.get("item_confidence"),
                              "condition_grade": item.get("condition_grade"),
                              "condition_reason": item.get("condition_reason"),
                              "valuation_reason": (item.get("valuation") or {}).get("reason")}
                             for item in review[:12]]}


def account_scans(account: dict, manifests: list[dict] | None = None) -> list[dict]:
    if not account.get("configured"):
        return []
    manifests = scan_manifests() if manifests is None else manifests
    owners = scheduled_scan_owners()
    return [scan for scan in manifests if
            (scan.get("account_id") or owners.get(scan.get("id"))) == account["id"]]


def account_scan_queue(account: dict, scans: list[dict]) -> list[dict]:
    """Include waiting keyword jobs before they have scan manifests."""
    if not account.get("configured"):
        return []
    active = [scan for scan in scans if scan.get("status") in {"queued", "running"}]
    waiting = []
    for slot in pending_slots():
        if slot["account_id"] != account["id"]:
            continue
        if slot.get("scan_mode") == "coverage_pilot":
            continue
        saved_item = slot.get("watch_item")
        if not saved_item:
            continue
        item = next((row for row in account.get("watch_items") or []
                     if row["term"].casefold() ==
                     str(saved_item.get("term") or "").casefold()), None)
        if item is None:
            continue
        waiting.append({"id": None, "status": "queued",
                        "created_at": slot["claimed_at"],
                        "zip_code": item["zip_code"],
                        "radius_miles": item["radius_miles"],
                        "keywords": [item["term"]], "watch_item": item,
                        "mode": (slot.get("scan_mode") or
                                 ("incremental" if slot.get("slot_key", "").startswith("fast-")
                                  else "scheduled")),
                        "retry_attempt": slot.get("retry_attempt") or 0,
                        "retry_at": slot.get("not_before")})
    return active + waiting


def account_scan_activity(account: dict, scans: list[dict],
                          queue: list[dict]) -> list[dict]:
    """Summarize each current watchlist keyword without mixing old settings."""
    def same_search(scan: dict, item: dict) -> bool:
        saved = scan.get("watch_item") or {}
        return (isinstance(saved, dict) and
                str(saved.get("term") or "").casefold() == item["term"].casefold() and
                all(saved.get(field) == item.get(field) for field in (
                    "zip_code", "radius_miles", "min_price", "max_price")))

    def created(scan: dict) -> float:
        try:
            return datetime.fromisoformat(
                str(scan.get("created_at") or "").replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError):
            return 0

    activity = []
    for item in account.get("watch_items") or []:
        history = sorted((scan for scan in scans if
                          scan.get("mode") in {"scheduled", "incremental"} and
                          same_search(scan, item)), key=created, reverse=True)
        latest = history[0] if history else None
        successful = next((scan for scan in history if
                           scan.get("status") == "complete"), None)
        waiting = sorted((scan for scan in queue if same_search(scan, item)),
                         key=created, reverse=True)
        queued = next((scan for scan in waiting if
                       scan.get("status") == "running"), None)
        queued = queued or (waiting[0] if waiting else None)
        current = queued or latest
        activity.append({
            "term": item["term"], "zip_code": item["zip_code"],
            "radius_miles": item["radius_miles"],
            "min_price": item["min_price"], "max_price": item["max_price"],
            "fast": bool(item.get("fast")),
            "status": current.get("status") if current else "not_started",
            "retry_attempt": current.get("retry_attempt", 0) if current else 0,
            "scan_id": current.get("id") if current else None,
            "last_scan_id": latest.get("id") if latest else None,
            "last_attempt_at": latest.get("created_at") if latest else None,
            "last_success_at": (successful.get("completed_at") or
                                successful.get("updated_at")) if successful else None,
            "cards_checked": latest.get("cards_checked") if latest else None,
            "new_cards_reviewed": (latest.get("scanned_count") if latest and
                                   latest.get("mode") == "incremental" else None),
            "items_shown": latest.get("result_count") if latest else None,
            "error": latest.get("error") if latest and
                     latest.get("status") == "failed" else None,
        })
    return activity


def result_path(manifest: dict) -> Path | None:
    relative = manifest.get("result_file")
    if not isinstance(relative, str) or not relative:
        return None
    path = (ROOT / relative).resolve()
    runs = (ROOT / "runs").resolve()
    return path if path.is_relative_to(runs) and path.is_file() else None


def result_listings(manifest: dict) -> tuple[dict, list[dict]]:
    source = result_path(manifest)
    if source is None:
        return {}, []
    data = json.loads(source.read_text(encoding="utf-8"))
    return data, data.get("listings") or []


def public_listing(scan_id: str, listing: dict, *, zip_code: str = "",
                   radius_miles: int | float | None = None) -> dict:
    vision = listing.get("vision") or {}
    observations = vision.get("observations") or {}
    condition = listing.get("condition_assessment") or {}
    result = dict(condition.get("result") or {})
    second_opinion = listing.get("second_opinion") or {}
    couch = listing.get("couch_quality") or {}
    couch_assessment = couch.get("assessment") or {}
    couch_guard = couch.get("guardrails") or {}
    couch_result = (couch.get("status") == "estimated" and bool(couch_guard))
    mixed_lot = bool(listing.get("mixed_lot")) or mixed_item_lot(
        listing.get("title") or "", listing.get("description") or "")
    pet_listing = live_animal_listing(listing.get("title") or "",
                                      listing.get("description") or "")
    ski_machine = (not mixed_lot and seller_named_ski_machine(listing, observations))
    valuation = ({"status": "unvalued", "reason": "pet_listing"} if pet_listing else
                 {"status": "unvalued", "reason": "mixed_item_lot"}
                 if mixed_lot else listing.get("valuation") or {})
    radius = listing.get("radius_check") or {}
    listing_id = str(listing.get("listing_id") or "")
    image_base = f"/api/scans/{scan_id}/images/{listing_id}"
    gallery = listing.get("image_files")
    photo_count = min(len(gallery), 4) if isinstance(gallery, list) and gallery else 1
    image_urls = ([f"{image_base}/{index}" for index in range(photo_count)]
                  if photo_count > 1 else [image_base])
    condition_guard_flags = (
        condition_guardrails(result, listing.get("title") or "",
                             listing.get("description") or "",
                             (vision.get("review_flags") or []), photo_count)
        if not pet_listing and condition.get("status") == "estimated" and
        {"usage_state", "condition_grade", "functional_status", "confidence"}
        .issubset(result) else [])
    vision_flags = vision.get("review_flags") or []
    listing_kind = ("promotion" if "store_promotion_not_single_item" in vision_flags
                    else observations.get("listing_kind") or "")
    review_flags = list(dict.fromkeys(
        vision_flags + (condition.get("review_flags") or []) + condition_guard_flags))
    if pet_listing:
        listing_kind = "product"
        review_flags.append("pet_listing")
    elif unidentified_lot(listing.get("title") or "",
                          listing.get("description") or ""):
        listing_kind = "mixed_or_unclear"
        review_flags.append("no_identifiable_item")
    if offer_override := seller_offer_override(listing.get("title") or "",
                                                listing.get("description") or ""):
        listing_kind = offer_override[0]
        review_flags.append(offer_override[1])
    if mixed_lot:
        review_flags.append("mixed_item_lot")
    if ski_machine and "ski" not in str(observations.get("item_type") or "").casefold():
        review_flags.append("seller_item_overrode_model_type")
    if second_opinion.get("review_required"):
        review_flags.append("second_opinion_disagreement")
    if couch_result and couch_guard.get("review_required"):
        review_flags.append("couch_quality_review_required")
    if (valuation.get("reason") == "item_identity_unconfirmed" and
            observations.get("item_type") and
            observations.get("category_confidence") in {"medium", "high"} and
            not set(review_flags).intersection({
                "item_identity_conflict", "seller_title_model_type_disagree"})):
        valuation = {**valuation, "reason": "price_group_unavailable",
                     "source_reason": "item_identity_unconfirmed"}
    card_price = listing.get("asking_price")
    asking_price, price_source, seller_price = display_price(
        card_price, listing.get("description") or "", review_flags,
        listing.get("title") or "")
    if (card_price == 0 and price_source == "facebook_card" and
            clear_free_offer(listing.get("title") or "",
                             listing.get("description") or "")):
        review_flags = [flag for flag in review_flags
                        if flag != "zero_price_card_may_not_be_free"]
    if price_source != "facebook_card" and "zero_price_card_may_not_be_free" not in review_flags:
        review_flags.append("zero_price_card_may_not_be_free")
    condition_uncertainties = ([] if mixed_lot or pet_listing else
                               list(result.get("uncertainties") or []))
    if photo_count > 1:
        condition_uncertainties = [
            f"{photo_count} listing photos cannot confirm hidden wear or function."
            if point.startswith("One small listing image cannot confirm") else point
            for point in condition_uncertainties]
    if mixed_lot:
        condition_uncertainties = ["Different items may have different conditions."]
    condition_grade = ("" if pet_listing else "unknown" if mixed_lot else
                       couch_guard.get("guarded_grade") if couch_result else
                       result.get("condition_grade")) or ""
    seller_wear_evidence = bool(re.search(
        r"\b(?:wear|worn|wore|stains?|scratches?|marks?|snags?|"
        r"scuffs?|dirty|cleaning|damage|damaged|weathering)\b",
        listing.get("description") or "", re.I))
    condition_reason = (
        "" if pet_listing else
        "Different items need separate condition checks." if mixed_lot else
        "Seller mentions wear or cleaning; Scout has not assigned a reliable grade."
        if condition_grade in {"", "unknown"} and seller_wear_evidence else
        "Condition is unconfirmed; check the listing photos and ask the seller."
        if condition_grade in {"", "unknown"} else
        "Seller says the item is for parts or cannot perform its intended function."
        if condition_grade == "needs_repair" and
        "repair_grade_from_seller_text" in review_flags else
        "Based on seller details and listing photos; confirm with the seller.")
    shown_title = display_title(
        listing.get("title") or listing.get("display_title") or "",
        listing.get("asking_price"),
        ("ski_machine" if ski_machine else observations.get("item_type") or ""),
        listing.get("description") or "")
    used_research = (used_source_plan(listing, display_name=shown_title,
                                      zip_code=zip_code,
                                      radius_miles=radius_miles)
                     if valuation.get("status") == "unvalued" and
                     listing_kind == "product" and not mixed_lot and
                     not pet_listing else None)
    return {
        "id": listing_id,
        "title": listing.get("title") or "",
        "display_title": shown_title,
        "description": listing.get("description") or "",
        "refactored_description": listing.get("refactored_description") or "",
        "generated_description": (mixed_lot_summary(listing.get("description") or "")
                                  if mixed_lot else listing.get("generated_description") or ""),
        "generated_description_status": listing.get("generated_description_status") or "",
        "location": listing.get("location") or "",
        "asking_price": asking_price,
        "card_asking_price": card_price,
        "seller_asking_price": seller_price,
        "price_source": price_source,
        "previous_price": listing.get("previous_price"),
        "vision_model": vision.get("model") or "",
        "condition_model": condition.get("model") or "",
        "second_opinion": ({
            "model": second_opinion.get("model"),
            "reasons": second_opinion.get("reasons") or [],
            "item_type_agrees": second_opinion.get("item_type_agrees"),
            "condition_grade_agrees": second_opinion.get("condition_grade_agrees"),
            "result": second_opinion.get("result") or {},
            "review_required": second_opinion.get("review_required", False),
        } if second_opinion.get("status") == "reviewed" else None),
        "url": listing.get("url") or "",
        "image_url": image_urls[0],
        "image_urls": image_urls,
        "distance_miles_approx": radius.get("distance_miles_approx"),
        "near_boundary": radius.get("near_boundary") or False,
        "item_type": ("assorted_items" if mixed_lot else "ski_machine" if ski_machine
                      else observations.get("item_type") or ""),
        "category": "pets" if pet_listing else "sports_equipment" if ski_machine else
                    observations.get("category") or "",
        "identity": "" if mixed_lot else observations.get("candidate_identity") or "",
        "identity_confidence": observations.get("identity_confidence") or "",
        "item_confidence": ("low" if mixed_lot else
                            observations.get("identity_confidence")
                            if observations.get("candidate_identity") else
                            observations.get("category_confidence")) or "low",
        "listing_kind": listing_kind,
        "pet_listing": pet_listing,
        "usage_state": "" if pet_listing else result.get("usage_state") or "",
        "condition_grade": condition_grade,
        "condition_reason": condition_reason,
        "condition_confidence": ("" if pet_listing else "low" if mixed_lot else
                                 couch_guard.get("guarded_confidence") if couch_result else
                                 result.get("confidence")) or "",
        "functional_status": "" if pet_listing else result.get("functional_status") or "",
        "condition_evidence": ([] if mixed_lot or pet_listing else
                               couch_guard.get("reasons") if couch_result else
                               result.get("evidence")) or [],
        "condition_uncertainties": condition_uncertainties,
        "mixed_lot": mixed_lot,
        "couch_quality": ({
            "model": couch.get("model"),
            "model_grade": couch_guard.get("model_grade"),
            "guarded_grade": couch_guard.get("guarded_grade"),
            "guarded_confidence": couch_guard.get("guarded_confidence"),
            "photo_kind": couch_assessment.get("photo_kind"),
            "model_color": couch_assessment.get("color"),
            "color_for_matching": couch_guard.get("color_for_matching"),
            "upholstery": couch_assessment.get("upholstery"),
            "visible_issues": couch_assessment.get("visible_issues") or [],
            "seller_reported_issues": couch_assessment.get("seller_reported_issues") or [],
            "reasons": couch_guard.get("reasons") or [],
            "review_required": couch_guard.get("review_required"),
        } if couch_result else None),
        "review_flags": review_flags,
        "valuation_status": valuation.get("status") or "",
        "price_confidence": ((valuation.get("price_category") or {}).get("confidence")
                             or "unrated"),
        "valuation": valuation,
        "used_research": used_research,
    }


def public_scan(manifest: dict) -> dict:
    data, listings = result_listings(manifest)
    items = [public_listing(manifest["id"], row,
                            zip_code=str(manifest.get("zip_code") or ""),
                            radius_miles=manifest.get("radius_miles"))
             for row in listings]
    analysis_state = ("complete" if manifest.get("status") == "complete" else
                      "incomplete" if manifest.get("status") in
                      {"failed", "interrupted"} else "preliminary")
    for item in items:
        item["analysis_state"] = analysis_state
    watch = manifest.get("watch_item") or {}
    products = [item for item in items if item["listing_kind"] == "product" and
                (not watch or (isinstance(item.get("asking_price"), (int, float)) and
                               not isinstance(item.get("asking_price"), bool) and
                               watch["min_price"] <= item["asking_price"] <=
                               watch["max_price"]))]
    shown_ids = {item["id"] for item in products}
    pending_ids = ({str(row.get("listing_id")) for row in listings
                    if not (row.get("vision") or {}).get("status")}
                   if manifest.get("status") == "running" else set())
    nearby_by_id = {str(row.get("listing_id")): row for row in listings}
    collection_file = SCAN_ROOT / str(manifest.get("id") or "") / "collection.json"
    if (re.fullmatch(r"scan-[a-z0-9-]+", str(manifest.get("id") or "")) and
            collection_file.is_file()):
        try:
            collected = json.loads(collection_file.read_text(encoding="utf-8"))
            scanned_rows = collected.get("listings") or []
        except (OSError, ValueError):
            scanned_rows = listings
    else:
        scanned_rows = listings

    def quick_card(row: dict, reason: str = "") -> dict:
        details = nearby_by_id.get(str(row.get("listing_id"))) or row
        description = str(details.get("description") or "").strip()
        outside_radius = str(row.get("listing_id")) not in nearby_by_id
        return {"id": str(row.get("listing_id") or ""),
                "title": str(row.get("title") or "Untitled listing")[:300],
                "url": str(details.get("url") or row.get("url") or ""),
                "description": description[:1200] or
                ("Seller description was not collected outside the radius."
                 if outside_radius else
                 "Seller description was unavailable."),
                "reason": reason}

    def filtered_reason(item: dict, row: dict) -> str:
        if item["listing_kind"] == "service":
            return ("Rental or hire offer" if
                    "rental_offer_not_item_sale" in item["review_flags"] else
                    "Service advertisement")
        if item["listing_kind"] == "promotion":
            return ("Financing offer; listed amount is a down payment"
                    if "financing_ad_not_item_price" in item["review_flags"] else
                    "Discount or business promotion")
        if item["listing_kind"] == "event_notice":
            return "Event notice"
        if watch:
            price = item.get("asking_price")
            if (isinstance(price, (int, float)) and not isinstance(price, bool) and
                    not watch["min_price"] <= price <= watch["max_price"]):
                source = ("Seller asks" if item.get("price_source") in
                          {"seller_description", "seller_title"} else "Listed")
                return (f"{source} ${price:g} is outside the saved "
                        f"${watch['min_price']}–${watch['max_price']} range")
            if item.get("price_source") == "unconfirmed":
                return "Facebook card says FREE, but seller text suggests a charge; price unclear"
        if "live_animal_not_product" in item["review_flags"]:
            return "Live animal rehoming post"
        if "no_identifiable_item" in item["review_flags"]:
            return "No identifiable item named by the seller"
        if (row.get("vision") or {}).get("status") == "error":
            return "AI photo analysis incomplete"
        if item["listing_kind"] == "product" and watch:
            return "Listed price unavailable for this watchlist range"
        return "No confirmed single physical item"

    scanned_cards = [quick_card(row) for row in scanned_rows]
    outside_rows = sorted(
        (row for row in data.get("excluded_listings") or []
         if isinstance((row.get("radius_check") or {}).get("distance_miles_approx"),
                       (int, float)) and
         not isinstance((row.get("radius_check") or {}).get("distance_miles_approx"), bool)
         and math.isfinite((row.get("radius_check") or {})["distance_miles_approx"])
         and not seller_offer_override(row.get("title") or "", "")),
        key=lambda row: row["radius_check"]["distance_miles_approx"])
    closest_outside = [
        {"id": str(row.get("listing_id") or ""),
         "title": str(row.get("title") or "Untitled listing")[:300],
         "url": str(row.get("url") or ""),
         "location": str(row.get("location") or "")[:120],
         "asking_price": row.get("asking_price"),
         "distance_miles": row["radius_check"]["distance_miles_approx"],
         "near_boundary": bool(row["radius_check"].get("near_boundary"))}
        for row in outside_rows[:3]]
    filtered_cards = [quick_card(row, filtered_reason(item, row))
                      for row, item in zip(listings, items)
                      if item["id"] not in shown_ids and
                      item["id"] not in pending_ids]
    return {
        **manifest,
        "stage_progress": scan_stage_progress(manifest),
        "listings": products,
        "result_count": len(products),
        "nearby_count": len(items),
        "filtered_out_count": len(filtered_cards),
        "pending_analysis_count": len(pending_ids),
        "scanned_cards": scanned_cards,
        "closest_outside": closest_outside,
        "unresolved_location_count": len(data.get("unresolved_listings") or []),
        "filtered_cards": filtered_cards,
        "ai_error_count": sum((row.get("vision") or {}).get("status") == "error"
                              for row in listings),
        "radius_basis": data.get("radius_filter_method") or "",
        "radius_limitations": data.get("radius_filter_limitations") or [],
    }


def actionable_alerts(payload: dict) -> dict:
    """Add current saved scan evidence to old and new alert rows."""
    by_scan = {}
    enriched = []
    for alert in payload.get("alerts") or []:
        scan_id = str(alert.get("scan_id") or "")
        if scan_id not in by_scan:
            try:
                manifest = read_manifest(scan_id)
                products = public_scan(manifest)["listings"]
                by_scan[scan_id] = {
                    "items": {str(item["id"]): item for item in products},
                    "keyword": ", ".join(manifest.get("keywords") or []),
                }
            except (OSError, ValueError, KeyError, TypeError):
                by_scan[scan_id] = {"items": {}, "keyword": ""}
        saved = by_scan[scan_id]
        item = saved["items"].get(str(alert.get("listing_id") or ""))
        if item:
            summary = alert_item_summary(item, saved["keyword"])
            alert = {**alert,
                     **{key: value for key, value in summary.items() if key != "id"},
                     "image_url": item.get("image_url")}
        enriched.append(alert)
    return {**payload, "alerts": enriched}


def start_scan(payload: dict, *, account_id: str | None = None,
               email_batch_key: str | None = None,
               email_batch_size: int = 1,
               scheduled_slot_key: str | None = None,
               retry_root: str | None = None,
               retry_attempt: int = 0,
               silent_pilot: bool = False,
               pilot_owner_id: str | None = None) -> dict:
    global active_scan_id
    watch_item = payload.get("watch_item")
    if watch_item and (not isinstance(watch_item, dict) or
                       payload.get("keywords") != [watch_item.get("term")]):
        raise ValueError("A watchlist scan needs exactly one matching keyword")
    with active_lock:
        if active_scan_id:
            raise RuntimeError("A scan is already running")
        manifest = create_manifest(
            payload.get("zip_code"), payload.get("radius_miles"),
            payload.get("keywords"), mode=payload.get("mode", "manual"),
            account_id=account_id)
        if scheduled_slot_key:
            manifest["scheduled_slot_key"] = scheduled_slot_key
            manifest["retry_root"] = retry_root
            manifest["retry_attempt"] = retry_attempt
            save_manifest(manifest)
        if silent_pilot:
            manifest["silent_pilot"] = True
            manifest["pilot_owner_id"] = pilot_owner_id
            save_manifest(manifest)
        if account_id and not silent_pilot:
            batch_key = email_batch_key or manifest["id"]
            if prepare_email_batch(account_id, batch_key, email_batch_size):
                manifest["email_batch_key"] = batch_key
                save_manifest(manifest)
        if watch_item:
            manifest["watch_term"] = watch_item["term"]
            manifest["watch_item"] = watch_item
            save_manifest(manifest)
        if not silent_pilot:
            settings = ROOT / "marketplace_settings.json"
            temporary = settings.with_suffix(".json.tmp")
            temporary.write_text(json.dumps({
                "zip_code": manifest["zip_code"],
                "radius_miles": manifest["radius_miles"],
                "keywords": manifest["keywords"],
            }, indent=2) + "\n", encoding="utf-8")
            temporary.replace(settings)
        active_scan_id = manifest["id"]

    launch_scan_worker(manifest)
    return manifest


def complete_scan_notifications(completed: dict) -> None:
    """Persist matches first, then close the digest for this scan's batch."""
    if completed.get("silent_pilot") or completed.get("status") not in {
            "complete", "failed"}:
        return
    if completed["status"] == "complete":
        try:
            result = ROOT / str(completed.get("result_file") or "")
            if result.name == "priced.json" and result.is_file():
                enqueue_price_research(completed["id"], result)
        except Exception as error:
            print(f"Could not queue price research for {completed.get('id')}: {error}",
                  flush=True)
    try:
        scan = public_scan(completed) if completed["status"] == "complete" else completed
        if completed["status"] == "complete":
            record_scan_matches(scan)
        email_ready = finish_email_scan(scan)
        try:
            persisted = read_manifest(completed["id"])
        except (FileNotFoundError, ValueError, TypeError):
            # A test or a scan created outside the normal manifest store.
            pass
        else:
            persisted["notifications_reconciled_at"] = datetime.now(timezone.utc).isoformat()
            save_manifest(persisted)
        if email_ready:
            deliver_pending_summaries()
    except Exception as error:
        print(f"Could not create alerts or email for {completed.get('id')}: {error}",
              flush=True)


def launch_scan_worker(manifest: dict) -> None:
    """Start a scan and make an unexpected worker failure visible immediately."""
    global active_scan_id
    scan_id = manifest["id"]

    def fail_scan(error: Exception) -> dict:
        print(f"Scan worker {scan_id} stopped unexpectedly: {error}", flush=True)
        try:
            latest = read_manifest(scan_id)
        except (OSError, ValueError, TypeError):
            latest = dict(manifest)
        # The pipeline may have saved its final result before a later step failed.
        if latest.get("status") not in {"complete", "failed"}:
            latest["status"] = "failed"
            latest["stage"] = "Scan worker stopped"
            latest["error"] = f"{type(error).__name__}: {error}"[:300]
            latest["completed_at"] = datetime.now(timezone.utc).isoformat()
            try:
                save_manifest(latest)
            except Exception as save_error:
                print(f"Could not save failed scan {scan_id}: {save_error}",
                      flush=True)
        return latest

    def worker() -> None:
        global active_scan_id
        try:
            try:
                completed = run_scan(scan_id)
            except Exception as error:
                completed = fail_scan(error)
            complete_scan_notifications(completed)
        finally:
            with active_lock:
                if active_scan_id == scan_id:
                    active_scan_id = None

    try:
        threading.Thread(target=worker, name=scan_id, daemon=True).start()
    except Exception as error:
        completed = fail_scan(error)
        with active_lock:
            if active_scan_id == scan_id:
                active_scan_id = None
        complete_scan_notifications(completed)
        raise RuntimeError("Could not start the scan worker") from error


def resume_interrupted_scan(scan_id: str) -> None:
    """Finish a scan in a replacement server after an intentional restart."""
    global active_scan_id
    manifest = read_manifest(scan_id)
    if manifest.get("status") != "interrupted":
        raise ValueError("Only an interrupted scan can be resumed")
    with active_lock:
        if active_scan_id:
            raise RuntimeError("A scan is already running")
        active_scan_id = scan_id

    launch_scan_worker(manifest)


def run_due_schedule(now=None) -> str | None:
    """Queue each profile's due slot and run pending jobs one at a time."""
    profiles = {row["id"]: row for row in list_account_profiles()}
    already_pending = [slot for slot in pending_slots()
                       if slot.get("scan_mode") == "coverage_pilot" or
                       (profiles.get(slot["account_id"]) and
                        any(row["term"].casefold() ==
                            str((slot.get("watch_item") or {}).get("term") or "").casefold()
                            for row in profiles[slot["account_id"]].get("watch_items") or []))]
    for account in profiles.values():
        if slot := due_slot(account, now):
            for item in account.get("watch_items") or []:
                claim_slot(watch_slot_key(slot, item["term"]),
                           account_id=account["id"], watch_item=item)
        if (fast_slot := due_fast_slot(account, now)) and not any(
                row["account_id"] == account["id"] for row in already_pending):
            item = next((row for row in account.get("watch_items") or []
                         if row.get("fast")), None)
            if item:
                claim_slot(watch_slot_key(fast_slot, item["term"]),
                           account_id=account["id"], watch_item=item)
    pilot = load_pilot()
    if pilot and pilot.get("status") == "running" and pilot.get("silent"):
        owner = profiles.get(pilot.get("account_id"))
        item = pilot.get("watch_item") or {}
        if owner and item.get("term"):
            pilot_account = {**owner, "plan": "plus_preview", "configured": True,
                             "watch_items": [{**item, "fast": True}]}
            fast_slot = due_fast_slot(pilot_account, now)
            if fast_slot and fast_slot.endswith("-00"):
                claim_slot(watch_slot_key(f"pilot:{pilot['id']}:{fast_slot}",
                                          item["term"]),
                           account_id=owner["id"], watch_item=item,
                           scan_mode="coverage_pilot")
    return start_next_pending(profiles)


def queue_failed_scan_retries(now: datetime | None = None) -> int:
    """Give recent failed account jobs one delayed retry, without replaying old scans."""
    now = now or datetime.now(timezone.utc)
    profiles = {account["id"]: account for account in list_account_profiles()}
    queued = 0
    for path in SCAN_ROOT.glob("scan-*/manifest.json"):
        try:
            if now.timestamp() - path.stat().st_mtime > 7200:
                continue
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if (manifest.get("status") != "failed" or
                    manifest.get("mode") not in {"scheduled", "incremental"} or
                    manifest.get("silent_pilot") or
                    not manifest.get("account_id") or
                    not isinstance(manifest.get("watch_item"), dict) or
                    int(manifest.get("retry_attempt") or 0) >= 1):
                continue
            failed_at = datetime.fromisoformat(manifest["updated_at"].replace("Z", "+00:00"))
            if failed_at.tzinfo is None or not timedelta(seconds=90) <= now - failed_at <= timedelta(hours=2):
                continue
            root = manifest.get("retry_root") or manifest["id"]
            item = manifest["watch_item"]
            account = profiles.get(manifest["account_id"])
            current = next((saved for saved in account.get("watch_items") or []
                            if saved["term"].casefold() ==
                            str(item.get("term") or "").casefold()), None) if account else None
            if (current is None or
                    (manifest["mode"] == "incremental" and
                     (account.get("plan") != "plus_preview" or not current.get("fast")))):
                continue
            slot = watch_slot_key(f"retry:{root}", item["term"])
            if claim_slot(slot, account_id=manifest["account_id"],
                          watch_item=current, scan_mode=manifest["mode"],
                          retry_root=root, retry_attempt=1,
                          not_before=(now + timedelta(seconds=30)).isoformat()):
                manifest["retry_scheduled_at"] = now.isoformat()
                save_manifest(manifest)
                queued += 1
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return queued


def start_next_pending(profiles: dict[str, dict] | None = None) -> str | None:
    profiles = profiles or {row["id"]: row for row in list_account_profiles()}
    if active_scan_id:
        return None
    existing_scans = scan_manifests()
    for pending in pending_slots():
        account_id = pending["account_id"]
        slot = pending["slot_key"]
        pilot_silent = pending.get("scan_mode") == "coverage_pilot"
        existing = next((scan for scan in existing_scans
                         if (scan.get("pilot_owner_id") if pilot_silent else
                             scan.get("account_id")) == account_id and
                         scan.get("scheduled_slot_key") == slot), None)
        if existing:
            finish_slot(slot, existing["id"], account_id=account_id)
            if existing.get("status") in {"queued", "running"}:
                return None
            continue
        account = profiles.get(account_id)
        if account is None:
            release_slot(slot, account_id=account_id)
            continue
        item = pending.get("watch_item")
        if item and not pilot_silent and not any(
                row["term"].casefold() == str(item.get("term") or "").casefold()
                for row in account.get("watch_items") or []):
            release_slot(slot, account_id=account_id)
            continue
        not_before = pending.get("not_before")
        if not_before:
            try:
                if datetime.now(timezone.utc) < datetime.fromisoformat(
                        not_before.replace("Z", "+00:00")):
                    continue
            except (TypeError, ValueError):
                release_slot(slot, account_id=account_id)
                continue
        try:
            if pilot_silent:
                pilot = load_pilot()
                if (not pilot or pilot.get("status") != "running" or
                        not pilot.get("silent") or
                        pilot.get("account_id") != account_id or
                        f"pilot:{pilot['id']}:" not in slot or
                        item != pilot.get("watch_item")):
                    release_slot(slot, account_id=account_id)
                    continue
            mode = ("incremental" if pilot_silent else pending.get("scan_mode")) or (
                "incremental" if slot.startswith("fast-") else "scheduled")
            fast = mode == "incremental"
            if fast and not pilot_silent and (not item or not item.get("fast") or
                         account.get("plan") != "plus_preview"):
                release_slot(slot, account_id=account_id)
                continue
            if item and not pilot_silent:
                current = next((row for row in account.get("watch_items") or []
                                if row["term"].casefold() == item["term"].casefold()), None)
                if current is None or (fast and not current.get("fast")):
                    release_slot(slot, account_id=account_id)
                    continue
                item = current
            pilot_kwargs = ({"silent_pilot": True, "pilot_owner_id": account_id}
                            if pilot_silent else {})
            manifest = start_scan({
                "zip_code": item["zip_code"] if item else account["zip_code"],
                "radius_miles": item["radius_miles"] if item else account["radius_miles"],
                "keywords": [item["term"]] if item else account["terms"],
                "watch_item": item,
                "mode": mode,
            }, account_id=(f"coverage:{account_id}" if pilot_silent else account_id),
                email_batch_key=slot.split(":item:", 1)[0],
                email_batch_size=(1 if fast else
                                  len(account.get("watch_items") or [1])
                                  if ":item:" in slot and not slot.startswith("retry:")
                                  else 1),
                scheduled_slot_key=slot,
                retry_root=pending.get("retry_root"),
                retry_attempt=pending.get("retry_attempt") or 0,
                **pilot_kwargs)
            finish_slot(slot, manifest["id"], account_id=account_id)
            return manifest["id"]
        except RuntimeError as error:
            if str(error) == "A scan is already running":
                return None
            raise
        except (ValueError, TypeError, KeyError):
            release_slot(slot, account_id=account_id)
            raise
        except Exception:
            # Keep the slot for the next scheduler pass after a temporary
            # database, file-system, or worker-launch failure.
            raise
    return None


def queue_account_scans(account: dict) -> dict:
    if active_scan_id:
        raise RuntimeError("A scan is already running")
    batch = "manual-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    items = account.get("watch_items") or []
    if not items:
        raise ValueError("Save at least one watchlist item first")
    first = items[0]
    manifest = start_scan({"zip_code": first["zip_code"],
                           "radius_miles": first["radius_miles"],
                           "keywords": [first["term"]],
                           "watch_item": first, "mode": "scheduled"},
                          account_id=account["id"],
                          email_batch_key=batch,
                          email_batch_size=len(items))
    for item in items[1:]:
        claim_slot(watch_slot_key(batch, item["term"]),
                   account_id=account["id"], watch_item=item)
    return {"id": manifest["id"], "status": "running",
            "queued_count": len(items)}


def scheduler_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            finish_pilot_if_due()
        except Exception as error:
            print(f"Coverage comparison could not finish: {error}", flush=True)
        try:
            reconcile_orphaned_scans()
        except Exception as error:
            print(f"Could not reconcile stopped scan: {error}", flush=True)
        try:
            queue_failed_scan_retries()
        except Exception as error:
            print(f"Failed scan could not be queued for retry: {error}", flush=True)
        try:
            run_due_schedule()
        except Exception as error:
            print(f"Scheduled scan did not start: {error}", flush=True)
        try:
            launch_price_research()
        except Exception as error:
            print(f"Price research did not start: {error}", flush=True)
        try:
            recover_email_notifications()
        except Exception as error:
            print(f"Scan notifications could not be reconciled: {error}", flush=True)
        try:
            deliver_pending_summaries()
        except Exception as error:
            print(f"Email delivery did not run: {error}", flush=True)
        stop.wait(30)


def launch_price_research() -> None:
    """Run one bounded area check only while normal scans are idle."""
    global price_research_active
    with active_lock:
        if price_research_active or active_scan_id or pending_slots():
            return
        job = claim_price_research()
        if not job:
            return
        price_research_active = True

    def worker() -> None:
        global price_research_active
        try:
            run_price_research(job)
        finally:
            with active_lock:
                price_research_active = False

    threading.Thread(target=worker, name=job["id"], daemon=True).start()


def reconcile_orphaned_scans(*, min_age_seconds: int = 90,
                            fail_incomplete: bool = True) -> None:
    """Finish saved results or end a scan whose worker is no longer active."""
    if active_scan_id:
        return
    now = datetime.now(timezone.utc)
    for manifest in scan_manifests():
        if manifest.get("status") not in {"queued", "running"}:
            continue
        try:
            updated = datetime.fromisoformat(str(manifest["updated_at"]).replace("Z", "+00:00"))
            if updated.tzinfo is None or now - updated < timedelta(seconds=min_age_seconds):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        scan_dir = SCAN_ROOT / manifest["id"]
        priced = scan_dir / "priced.json"
        if priced.is_file():
            try:
                data = json.loads(priced.read_text(encoding="utf-8"))
                if not isinstance(data.get("listings"), list):
                    raise ValueError("Pricing result has no listings")
                review = scan_dir / "review.csv"
                if not review.is_file():
                    export_queue(priced, review)
                manifest["result_file"] = str(priced.relative_to(ROOT))
                manifest["status"] = "complete"
                manifest["stage"] = "Complete"
                manifest["completed_at"] = now.isoformat()
                manifest["recovered_after_worker_stop"] = True
            except (OSError, ValueError, KeyError, TypeError) as error:
                if not fail_incomplete:
                    continue
                manifest["status"] = "failed"
                manifest["stage"] = "Scan stopped before completion"
                manifest["error"] = str(error)[:300]
        elif fail_incomplete:
            manifest["status"] = "failed"
            manifest["stage"] = "Scan stopped before completion"
            manifest["error"] = "The scan worker stopped. Start a new scan to retry."
        else:
            continue
        save_manifest(manifest)
        complete_scan_notifications(manifest)


def mark_interrupted_scans() -> None:
    resume_id = os.environ.get("SCOUT_RESUME_SCAN_ID")
    for manifest in scan_manifests():
        if manifest.get("status") in {"queued", "running"}:
            retryable = (manifest.get("id") != resume_id and
                         manifest.get("mode") in {"scheduled", "incremental"} and
                         manifest.get("account_id") and
                         isinstance(manifest.get("watch_item"), dict))
            manifest["status"] = "failed" if retryable else "interrupted"
            manifest["stage"] = "Scan stopped before completion" if retryable else (
                "Interrupted when the local server stopped")
            if retryable:
                manifest["error"] = "Scout restarted while this scan was running."
            save_manifest(manifest)
            if retryable:
                complete_scan_notifications(manifest)


def recover_email_notifications() -> None:
    # This runs on every scheduler pass, so read only tiny manifest files.
    # scan_manifests() also loads every completed scan's listing results.
    for path in SCAN_ROOT.glob("scan-*/manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(manifest, dict):
            continue
        if (manifest.get("status") in {"complete", "failed"} and
                not manifest.get("silent_pilot") and
                not manifest.get("notifications_reconciled_at")):
            complete_scan_notifications(manifest)


class Handler(BaseHTTPRequestHandler):
    def _public_request(self) -> bool:
        path = unquote(urlsplit(self.path).path)
        return bool(PUBLIC_HOST and self.headers.get("Host", "").lower() == PUBLIC_HOST
                    and route_path(path)[1] in {PUBLIC_PREFIX, LEGACY_PUBLIC_PREFIX})

    def _local_request(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_port}"
        host = self.headers.get("Host", "").lower()
        if host in {expected, f"localhost:{self.server.server_port}"}:
            return True
        return self._public_request()

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        allowed = ({f"https://{PUBLIC_HOST}"}
                   if PUBLIC_HOST and self.headers.get("Host", "").lower() == PUBLIC_HOST
                   else {f"http://127.0.0.1:{self.server.server_port}",
                         f"http://localhost:{self.server.server_port}"})
        return not origin or origin in allowed

    def _identity(self) -> dict[str, str] | None:
        if not self._public_request():
            return None
        return verify_access_token(self.headers.get("Cf-Access-Jwt-Assertion"),
                                   CF_TEAM_DOMAIN, CF_AUD)

    def _account(self, email: str | None) -> dict:
        return get_account(email, allow_legacy_claim=not self._public_request())

    def _is_admin(self, identity: dict[str, str] | None) -> bool:
        return bool(identity and is_admin_email(identity["email"]))

    def _owns_scan(self, scan_id: str, email: str) -> bool:
        if self._public_request() and is_admin_email(email):
            return True
        account = self._account(email)
        if not account["configured"]:
            return False
        return any(scan["id"] == scan_id for scan in account_scans(account))

    def _send(self, status: int, body: bytes, mime: str,
              *, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self'; script-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict | list) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def do_GET(self) -> None:
        if not self._local_request():
            return self._error(403, "Local requests only")
        try:
            identity = self._identity()
        except AccessDenied as error:
            return self._error(401, str(error))
        request = urlsplit(self.path)
        path, base_path = route_path(unquote(request.path))
        if self._public_request() and base_path == LEGACY_PUBLIC_PREFIX:
            destination = PUBLIC_PREFIX + path
            if request.query:
                destination += "?" + request.query
            self.send_response(308)
            self.send_header("Location", destination)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self._public_request() and path == "/api/searches":
            return self._error(403, "This page is available only on this computer")
        if self._public_request() and (path in {
                "/admin", "/api/admin", "/price-research",
                "/api/price-research", "/comparables/couches",
                "/api/comparables/couches", "/comparables/general",
                "/api/comparables/general"} or COUCH_IMAGE_ROUTE.fullmatch(path)
                or GENERAL_IMAGE_ROUTE.fullmatch(path)) and not self._is_admin(identity):
            return self._error(403, "Admin access required")
        if self._public_request() and not PUBLIC_WRITES and path in {
                "/setup", "/api/account", "/api/alerts"}:
            return self._error(403, "Public access is view-only")
        email = (identity["email"] if identity else
                 self.headers.get("X-Scout-Email") or
                 parse_qs(request.query).get("email", [None])[0])
        if path == "/api/scans":
            try:
                account = self._account(email) if email else None
                scans = (account_scans(account) if account else
                         ([] if self._public_request() else scan_manifests()))
                queue = account_scan_queue(account, scans) if account else []
            except ValueError as error:
                return self._error(400, str(error))
            return self._json(200, {"scans": scans, "queue": queue,
                                    "activity": (account_scan_activity(account, scans, queue)
                                                 if account else []),
                                    "coverage_pilot": (public_pilot_status(
                                        account["id"], watch_items=account.get("watch_items"))
                                                       if account else None),
                                    "active_scan_id": active_scan_id})
        if path in {"/api/health", "/api/health/export.csv"}:
            try:
                account = self._account(email) if email else {
                    "configured": False, "watch_items": [], "search_id": None}
                summaries = scan_summaries(SCAN_ROOT)
                scans = account_scans(account, summaries) if account.get("configured") else []
                queue = account_scan_queue(account, scans) if account.get("configured") else []
                events = health_alert_events(account.get("search_id"), ROOT / DEFAULT_DB)
                feedback = health_feedback_events(account.get("id"), ROOT / DEFAULT_DB)
                benchmark = load_benchmark()
                if path == "/api/health/export.csv":
                    body = benchmark_csv(scans, account.get("watch_items") or [],
                                         benchmark, events).encode("utf-8")
                    return self._send(200, body, "text/csv; charset=utf-8")
                return self._json(200, health_report(account, scans, queue,
                                                     events, benchmark, feedback))
            except ValueError as error:
                return self._error(400, str(error))
        if path == "/api/config":
            if self._public_request():
                return self._error(403, "Open an email profile to view your searches")
            settings_path = ROOT / "marketplace_settings.json"
            if not settings_path.is_file():
                settings_path = ROOT / "marketplace_settings.example.json"
            settings = json.loads(
                settings_path.read_text(encoding="utf-8"))
            return self._json(200, settings)
        if path == "/api/account":
            try:
                account = self._account(email)
                if identity:
                    account = {**account, "role": "admin" if self._is_admin(identity) else "user"}
                return self._json(200, account)
            except ValueError as error:
                return self._error(400, str(error))
        if path == "/api/admin":
            return self._json(200, {**admin_overview(),
                                    "admin_emails": list_admin_emails(),
                                    "email_delivery": email_status(),
                                    "quality": quality_overview()})
        if path == "/api/price-research":
            return self._json(200, price_research_queue())
        if path == "/api/comparables/couches":
            try:
                return self._json(200, couch_comparables())
            except (OSError, ValueError, KeyError, TypeError):
                return self._error(404, "Couch price review is unavailable")
        if path == "/api/comparables/general":
            try:
                return self._json(200, general_comparables())
            except (OSError, ValueError, KeyError, TypeError):
                return self._error(404, "General price review is unavailable")
        if match := GENERAL_IMAGE_ROUTE.fullmatch(path):
            try:
                rows = [row for report in general_comparable_reports()
                        for row in report.get("listings") or []]
            except (OSError, ValueError, KeyError, TypeError):
                return self._error(404, "General price review is unavailable")
            if not any(str(row.get("listing_id")) == match.group(1) for row in rows):
                return self._error(404, "Image not found")
            found = get_image(ROOT / DEFAULT_DB, "facebook_marketplace", match.group(1))
            if not found:
                return self._error(404, "Saved image unavailable")
            return self._send(200, found[1], found[0])
        if match := COUCH_IMAGE_ROUTE.fullmatch(path):
            try:
                rows = json.loads(COUCH_DETAILS.read_text(encoding="utf-8"))["listings"]
            except (OSError, ValueError, KeyError, TypeError):
                return self._error(404, "Couch price review is unavailable")
            if not any(str(row.get("listing_id")) == match.group(1) for row in rows):
                return self._error(404, "Image not found")
            stored = get_image(ROOT / DEFAULT_DB, "facebook_marketplace",
                               match.group(1))
            if stored:
                return self._send(200, stored[1], stored[0])
            return self._error(404, "Image not found")
        if path == "/api/searches":
            return self._json(200, {"searches": list_searches()})
        if path == "/api/alerts":
            triage_status = parse_qs(request.query).get("view", ["inbox"])[0]
            if triage_status not in {"inbox", "saved", "hidden"}:
                return self._error(400, "Choose Inbox, Saved, or Hidden")
            try:
                page = int(parse_qs(request.query).get("page", ["1"])[0])
                if not 1 <= page <= 10000:
                    raise ValueError
            except ValueError:
                return self._error(400, "Choose a valid alert page")
            pagination = {"limit": 30, "offset": (page - 1) * 30,
                          "triage_status": triage_status}
            if email:
                try:
                    account = self._account(email)
                except ValueError as error:
                    return self._error(400, str(error))
                if not account["configured"]:
                    return self._json(200, {"unread_count": 0, "alerts": []})
                scan_ids = {scan["id"] for scan in account_scans(account)}
                return self._json(200, actionable_alerts(list_alerts(
                    search_id=account["search_id"], scan_ids=scan_ids,
                    **pagination)))
            return self._json(200, {"unread_count": 0, "alerts": []}
                              if self._public_request() else actionable_alerts(
                                  list_alerts(**pagination)))
        if match := SCAN_FEEDBACK_ROUTE.fullmatch(path):
            if not email or not self._owns_scan(match.group(1), email):
                return self._error(404, "Scan not found")
            account = self._account(email)
            if not account["configured"]:
                return self._json(200, {"feedback": {}})
            try:
                scan = public_scan(read_manifest(match.group(1)))
            except (FileNotFoundError, ValueError):
                return self._error(404, "Scan not found")
            return self._json(200, {"feedback": feedback_for_listings(
                account["id"], [item["id"] for item in scan["listings"]],
                ROOT / DEFAULT_DB)})
        if match := IMAGE_ROUTE.fullmatch(path):
            if identity and not self._owns_scan(match.group(1), email):
                return self._error(404, "Scan not found")
            try:
                manifest = read_manifest(match.group(1))
                source = result_path(manifest)
                if source is None:
                    return self._error(404, "Scan results are not available yet")
                _, listings = result_listings(manifest)
                row = next((item for item in listings
                            if str(item.get("listing_id")) == match.group(2)), None)
                if row is None:
                    return self._error(404, "Image not found")
                gallery = row.get("image_files")
                photo_index = int(match.group(3)) if match.group(3) is not None else None
                if photo_index is not None:
                    files = gallery if isinstance(gallery, list) and gallery else [row.get("image_file")]
                    if photo_index >= len(files):
                        return self._error(404, "Image not found")
                    relative = files[photo_index]
                else:
                    relative = row.get("image_file")
                if isinstance(relative, str):
                    image = (source.parent / relative).resolve()
                    if image.is_relative_to(source.parent.resolve()) and image.is_file():
                        mime = mimetypes.guess_type(image.name)[0] or "image/jpeg"
                        return self._send(200, image.read_bytes(), mime)
                if photo_index is None:
                    stored = get_image(ROOT / DEFAULT_DB, "facebook_marketplace",
                                       match.group(2))
                else:
                    photos = get_images(ROOT / DEFAULT_DB, "facebook_marketplace",
                                        match.group(2))
                    stored = photos[photo_index] if photo_index < len(photos) else None
                if stored:
                    return self._send(200, stored[1], stored[0])
                return self._error(404, "Image not found")
            except (FileNotFoundError, ValueError):
                return self._error(404, "Scan not found")
        if match := USED_CANDIDATES_ROUTE.fullmatch(path):
            scan_id, listing_id = match.groups()
            if identity and not self._owns_scan(scan_id, email):
                return self._error(404, "Scan not found")
            try:
                manifest = read_manifest(scan_id)
                item = next((row for row in public_scan(manifest)["listings"]
                             if row["id"] == listing_id), None)
                plan = item.get("used_research") if item else None
                if not plan:
                    return self._error(404, "Used price research is unavailable for this item")
                result = search_craigslist(
                    plan["query"], str(manifest.get("zip_code") or ""),
                    int(manifest.get("radius_miles") or 0))
                return self._json(200, result)
            except (FileNotFoundError, KeyError, TypeError, ValueError):
                return self._error(404, "Scan not found")
            except SearchLimitReached as error:
                return self._error(429, str(error))
            except SearchUnavailable as error:
                return self._error(502, str(error))
        if match := SCAN_ROUTE.fullmatch(path):
            if identity and not self._owns_scan(match.group(1), email):
                return self._error(404, "Scan not found")
            try:
                return self._json(200, public_scan(read_manifest(match.group(1))))
            except (FileNotFoundError, ValueError):
                return self._error(404, "Scan not found")
        static = {
            "/": "index.html", "/setup": "index.html",
            "/health": "index.html",
            "/admin": "index.html", "/comparables/couches": "index.html",
            "/price-research": "index.html",
            "/comparables/general": "index.html",
            "/index.html": "index.html",
            "/styles.css": "styles.css", "/app.js": "app.js",
            "/trip_math.js": "trip_math.js",
        }
        name = static.get(path) or ("index.html" if PAGE_ROUTE.fullmatch(path) else None)
        if name is None:
            return self._error(404, "Page not found")
        if identity and (match := PAGE_ROUTE.fullmatch(path)) and not self._owns_scan(match.group(1), email):
            return self._error(404, "Scan not found")
        file = WEB / name
        mime = {".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8"}[file.suffix]
        body = file.read_bytes()
        if name == "index.html":
            body = body.replace(b"__BASE_PATH__", base_path.encode("ascii"))
            body = body.replace(b"__PUBLIC_WRITES__",
                                b"true" if base_path and PUBLIC_WRITES else b"false")
            for asset, marker in (("styles.css", b"__CSS_VERSION__"),
                                  ("app.js", b"__JS_VERSION__"),
                                  ("trip_math.js", b"__TRIP_VERSION__")):
                digest = hashlib.sha256((WEB / asset).read_bytes()).hexdigest()[:12]
                body = body.replace(marker, digest.encode("ascii"))
        self._send(200, body, mime, cache="no-store" if name == "index.html" else "no-cache")

    def do_POST(self) -> None:
        if not self._local_request():
            return self._error(403, "Local requests only")
        try:
            identity = self._identity()
        except AccessDenied as error:
            return self._error(401, str(error))
        if self._public_request() and not PUBLIC_WRITES:
            return self._error(403, "Public access is view-only")
        if not self._same_origin():
            return self._error(403, "Cross-site requests are blocked")
        path, _ = route_path(unquote(urlsplit(self.path).path))
        if identity and (path == "/api/searches" or SEARCH_SCAN_ROUTE.fullmatch(path)):
            return self._error(403, "This action is available only on this computer")
        if path not in {"/api/scans", "/api/searches", "/api/account",
                        "/api/account/open",
                        "/api/account/scan", "/api/alerts/hide-all"} and not (
                SEARCH_SCAN_ROUTE.fullmatch(path) or ALERT_READ_ROUTE.fullmatch(path)
                or ALERT_TRIAGE_ROUTE.fullmatch(path)
                or ITEM_FEEDBACK_ROUTE.fullmatch(path)):
            return self._error(404, "Page not found")
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            return self._error(415, "Send JSON scan settings")
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if not 0 < length <= MAX_BODY:
                return self._error(413, "Request is too large or empty")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                return self._error(400, "Enter settings")
            if path == "/api/scans":
                email = identity["email"] if identity else payload.get("email")
                account = self._account(email) if email else None
                if email and not account["configured"]:
                    return self._error(400, "Save this email profile first")
                return self._json(202, start_scan(
                    payload, account_id=account["id"] if account else None))
            if path == "/api/account":
                return self._json(200, save_account(
                    {**payload, "email": identity["email"]} if identity else payload,
                    allow_legacy_claim=not bool(identity)))
            if path == "/api/account/open":
                return self._json(200, open_account(
                    identity["email"] if identity else payload.get("email"),
                    allow_legacy_claim=not bool(identity)))
            if path == "/api/account/scan":
                email = identity["email"] if identity else payload.get("email")
                account = self._account(email) if email else None
                if account is None:
                    return self._error(400, "Enter an email address first")
                if not account["configured"]:
                    return self._error(400, "Save your plan setup first")
                return self._json(202, queue_account_scans(account))
            if path == "/api/alerts/hide-all":
                email = identity["email"] if identity else payload.get("email")
                if not email:
                    return self._error(400, "Open an email profile first")
                account = self._account(email)
                if not account["configured"]:
                    return self._error(404, "Alert inbox not found")
                hidden = hide_all_inbox_alerts(account["search_id"])
                return self._json(200, {"hidden": hidden})
            if match := ITEM_FEEDBACK_ROUTE.fullmatch(path):
                scan_id, listing_id = match.groups()
                email = identity["email"] if identity else payload.get("email")
                if not email or not self._owns_scan(scan_id, email):
                    return self._error(404, "Scan not found")
                account = self._account(email)
                if not account["configured"]:
                    return self._error(404, "Scan not found")
                try:
                    scan = public_scan(read_manifest(scan_id))
                except (FileNotFoundError, ValueError):
                    return self._error(404, "Scan not found")
                if not any(item["id"] == listing_id for item in scan["listings"]):
                    return self._error(404, "Item not found")
                saved = save_feedback(account["id"], scan_id, listing_id,
                                      payload.get("vote"), payload.get("issue") or "",
                                      ROOT / DEFAULT_DB,
                                      correction=payload.get("correction"))
                return self._json(200, {"feedback": saved})
            if path == "/api/searches":
                return self._json(201, create_search(payload))
            if match := SEARCH_SCAN_ROUTE.fullmatch(path):
                search = get_search(int(match.group(1)))
                if search is None:
                    return self._error(404, "Saved search not found")
                return self._json(202, start_scan({
                    "zip_code": search["zip_code"],
                    "radius_miles": search["radius_miles"],
                    "keywords": search["scan_keywords"],
                }, account_id=account_id_for_search(search["id"])))
            if match := ALERT_TRIAGE_ROUTE.fullmatch(path):
                alert_id = int(match.group(1))
                email = identity["email"] if identity else payload.get("email")
                if email:
                    account = self._account(email)
                    if not account["configured"]:
                        return self._error(404, "Alert not found")
                    saved = set_alert_triage(
                        alert_id, payload.get("status"),
                        search_id=account["search_id"])
                else:
                    saved = set_alert_triage(alert_id, payload.get("status"))
                if saved:
                    return self._json(200, {"status": payload["status"]})
                return self._error(404, "Alert not found")
            alert_id = int(ALERT_READ_ROUTE.fullmatch(path).group(1))
            email = identity["email"] if identity else payload.get("email")
            if email:
                account = self._account(email)
                if not account["configured"]:
                    return self._error(404, "Alert not found")
                saved = mark_read(alert_id, search_id=account["search_id"])
            else:
                saved = mark_read(alert_id)
            if saved:
                return self._json(200, {"read": True})
            return self._error(404, "Alert not found")
        except (ValueError, TypeError) as error:
            return self._error(400, str(error))
        except RuntimeError as error:
            return self._error(409, str(error))

    def do_DELETE(self) -> None:
        if not self._local_request() or not self._same_origin():
            return self._error(403, "Local same-origin requests only")
        if self._public_request():
            return self._error(403, "This action is available only on this computer")
        path, _ = route_path(unquote(urlsplit(self.path).path))
        match = SEARCH_ROUTE.fullmatch(path)
        if match is None:
            return self._error(404, "Page not found")
        if delete_search(int(match.group(1))):
            return self._json(200, {"deleted": True})
        return self._error(404, "Saved search not found")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Marketplace scan website")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    reconcile_orphaned_scans(min_age_seconds=0, fail_incomplete=False)
    mark_interrupted_scans()
    recover_price_research()
    removed = remove_nonproduct_alerts()
    if removed:
        print(f"Removed {removed} service or financing alerts", flush=True)
    try:
        refresh_price_research_eligibility()
        refresh_price_research()
    except Exception as error:
        print(f"Could not refresh saved price research: {error}", flush=True)
    recover_email_notifications()
    if resume_id := os.environ.get("SCOUT_RESUME_SCAN_ID"):
        resume_interrupted_scan(resume_id)
    scheduler_stop = threading.Event()
    scheduler = threading.Thread(target=scheduler_loop, args=(scheduler_stop,),
                                 name="scout-scheduler", daemon=True)
    scheduler.start()
    print(f"Marketplace site: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler_stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
