"""Local website and API for Marketplace scans and saved AI results."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from datetime import datetime, timezone
import mimetypes
from pathlib import Path
import re
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from marketplace_alerts import (
    DEMO_ACCOUNT, account_id_for_search, claim_slot, create_search,
    delete_search, due_slot, finish_slot, get_account, get_search,
    list_account_profiles, list_admin_emails, list_alerts, list_searches,
    mark_read, open_account, is_admin_email, pending_slots, record_scan_matches,
    release_slot, save_account,
    scheduled_scan_owners,
    watch_slot_key,
)
from marketplace_db import DEFAULT_DB, get_image
from marketplace_access import AccessDenied, verify_access_token
from marketplace_listing_copy import display_title
from marketplace_price import display_price
from marketplace_scan_runner import (
    ROOT, SCAN_ROOT, create_manifest, read_manifest, run_scan, save_manifest,
)


WEB = ROOT / "marketplace_site"
PUBLIC_PREFIX = "/Facebooknotify"
PUBLIC_HOST = os.environ.get("SCOUT_PUBLIC_HOST", "").strip().lower()
PUBLIC_WRITES = os.environ.get("SCOUT_PUBLIC_WRITES", "") == "1"
CF_TEAM_DOMAIN = os.environ.get("SCOUT_CF_TEAM_DOMAIN", "").strip().lower()
CF_AUD = os.environ.get("SCOUT_CF_AUD", "").strip()
IMAGE_ROUTE = re.compile(r"^/api/scans/(scan-[a-z0-9-]+)/images/(\d+)$")
SCAN_ROUTE = re.compile(r"^/api/scans/(scan-[a-z0-9-]+)$")
PAGE_ROUTE = re.compile(r"^/scan/(scan-[a-z0-9-]+)$")
SEARCH_ROUTE = re.compile(r"^/api/searches/(\d+)$")
SEARCH_SCAN_ROUTE = re.compile(r"^/api/searches/(\d+)/scan$")
ALERT_READ_ROUTE = re.compile(r"^/api/alerts/(\d+)/read$")
MAX_BODY = 16_384
active_lock = threading.Lock()
active_scan_id: str | None = None


def route_path(path: str) -> tuple[str, str]:
    """Map the optional Cloudflare path prefix to Scout's local routes."""
    if path == PUBLIC_PREFIX:
        return "/", PUBLIC_PREFIX
    if path.startswith(PUBLIC_PREFIX + "/"):
        return path[len(PUBLIC_PREFIX):], PUBLIC_PREFIX
    return path, ""


def scan_manifests() -> list[dict]:
    SCAN_ROOT.mkdir(parents=True, exist_ok=True)
    records = []
    for path in SCAN_ROOT.glob("scan-*/manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            progress = scan_stage_progress(manifest)
            if progress:
                manifest["stage_progress"] = progress
            if manifest.get("status") == "complete":
                try:
                    manifest["result_count"] = public_scan(manifest)["result_count"]
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
        rows = json.loads(path.read_text(encoding="utf-8")).get("listings") or []
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(rows, list):
        return None
    processed = sum(isinstance(row, dict) and
                    bool((row.get(field) or {}).get("status")) for row in rows)
    total = int(manifest.get("within_radius_count") or len(rows))
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
        item = slot.get("watch_item")
        if item is None:
            continue
        waiting.append({"id": None, "status": "queued",
                        "created_at": slot["claimed_at"],
                        "zip_code": item["zip_code"],
                        "radius_miles": item["radius_miles"],
                        "keywords": [item["term"]], "watch_item": item})
    return active + waiting


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


def public_listing(scan_id: str, listing: dict) -> dict:
    vision = listing.get("vision") or {}
    observations = vision.get("observations") or {}
    condition = listing.get("condition_assessment") or {}
    result = condition.get("result") or {}
    valuation = listing.get("valuation") or {}
    radius = listing.get("radius_check") or {}
    listing_id = str(listing.get("listing_id") or "")
    vision_flags = vision.get("review_flags") or []
    listing_kind = ("promotion" if "store_promotion_not_single_item" in vision_flags
                    else observations.get("listing_kind") or "")
    review_flags = list(dict.fromkeys(
        vision_flags + (condition.get("review_flags") or [])))
    card_price = listing.get("asking_price")
    asking_price, price_source, seller_price = display_price(
        card_price, listing.get("description") or "", review_flags)
    if price_source != "facebook_card" and "zero_price_card_may_not_be_free" not in review_flags:
        review_flags.append("zero_price_card_may_not_be_free")
    return {
        "id": listing_id,
        "title": listing.get("title") or "",
        "display_title": listing.get("display_title") or display_title(
            listing.get("title") or "", listing.get("asking_price"),
            observations.get("item_type") or ""),
        "description": listing.get("description") or "",
        "refactored_description": listing.get("refactored_description") or "",
        "generated_description": listing.get("generated_description") or "",
        "generated_description_status": listing.get("generated_description_status") or "",
        "location": listing.get("location") or "",
        "asking_price": asking_price,
        "card_asking_price": card_price,
        "seller_asking_price": seller_price,
        "price_source": price_source,
        "previous_price": listing.get("previous_price"),
        "vision_model": vision.get("model") or "",
        "condition_model": condition.get("model") or "",
        "url": listing.get("url") or "",
        "image_url": f"/api/scans/{scan_id}/images/{listing_id}",
        "distance_miles_approx": radius.get("distance_miles_approx"),
        "near_boundary": radius.get("near_boundary") or False,
        "item_type": observations.get("item_type") or "",
        "category": observations.get("category") or "",
        "identity": observations.get("candidate_identity") or "",
        "identity_confidence": observations.get("identity_confidence") or "",
        "listing_kind": listing_kind,
        "usage_state": result.get("usage_state") or "",
        "condition_grade": result.get("condition_grade") or "",
        "condition_confidence": result.get("confidence") or "",
        "functional_status": result.get("functional_status") or "",
        "condition_evidence": result.get("evidence") or [],
        "condition_uncertainties": result.get("uncertainties") or [],
        "review_flags": review_flags,
        "valuation_status": valuation.get("status") or "",
        "valuation": valuation,
    }


def public_scan(manifest: dict) -> dict:
    data, listings = result_listings(manifest)
    items = [public_listing(manifest["id"], row) for row in listings]
    watch = manifest.get("watch_item") or {}
    products = [item for item in items if item["listing_kind"] == "product" and
                (not watch or (isinstance(item.get("asking_price"), (int, float)) and
                               not isinstance(item.get("asking_price"), bool) and
                               watch["min_price"] <= item["asking_price"] <=
                               watch["max_price"]))]
    shown_ids = {item["id"] for item in products}
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
        if watch:
            price = item.get("asking_price")
            if (isinstance(price, (int, float)) and not isinstance(price, bool) and
                    not watch["min_price"] <= price <= watch["max_price"]):
                source = "Seller asks" if item.get("price_source") == "seller_description" else "Listed"
                return (f"{source} ${price:g} is outside the saved "
                        f"${watch['min_price']}–${watch['max_price']} range")
            if item.get("price_source") == "unconfirmed":
                return "Facebook card says FREE, but seller text says it is not free; price unclear"
        if item["listing_kind"] == "service":
            return "Service advertisement"
        if item["listing_kind"] == "promotion":
            return "Discount or business promotion"
        if item["listing_kind"] == "event_notice":
            return "Event notice"
        if (row.get("vision") or {}).get("status") == "error":
            return "AI photo classification timed out"
        if item["listing_kind"] == "product" and watch:
            return "Listed price unavailable for this watchlist range"
        return "No confirmed single physical item"

    scanned_cards = [quick_card(row) for row in scanned_rows]
    filtered_cards = [quick_card(row, filtered_reason(item, row))
                      for row, item in zip(listings, items)
                      if item["id"] not in shown_ids]
    return {
        **manifest,
        "stage_progress": scan_stage_progress(manifest),
        "listings": products,
        "result_count": len(products),
        "nearby_count": len(items),
        "filtered_out_count": len(items) - len(products),
        "scanned_cards": scanned_cards,
        "filtered_cards": filtered_cards,
        "ai_error_count": sum((row.get("vision") or {}).get("status") == "error"
                              for row in listings),
        "radius_basis": data.get("radius_filter_method") or "",
        "radius_limitations": data.get("radius_filter_limitations") or [],
    }


def start_scan(payload: dict, *, account_id: str | None = None) -> dict:
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
        if watch_item:
            manifest["watch_term"] = watch_item["term"]
            manifest["watch_item"] = watch_item
            save_manifest(manifest)
        settings = ROOT / "marketplace_settings.json"
        temporary = settings.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({
            "zip_code": manifest["zip_code"],
            "radius_miles": manifest["radius_miles"],
            "keywords": manifest["keywords"],
        }, indent=2) + "\n", encoding="utf-8")
        temporary.replace(settings)
        active_scan_id = manifest["id"]

    def worker() -> None:
        global active_scan_id
        try:
            completed = run_scan(manifest["id"])
            if completed["status"] == "complete":
                try:
                    record_scan_matches(public_scan(completed))
                except Exception as error:
                    print(f"Could not create alerts for {manifest['id']}: {error}",
                          flush=True)
        finally:
            with active_lock:
                active_scan_id = None

    threading.Thread(target=worker, name=manifest["id"], daemon=True).start()
    return manifest


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

    def worker() -> None:
        global active_scan_id
        try:
            completed = run_scan(scan_id)
            if completed["status"] == "complete":
                try:
                    record_scan_matches(public_scan(completed))
                except Exception as error:
                    print(f"Could not create alerts for {scan_id}: {error}",
                          flush=True)
        finally:
            with active_lock:
                active_scan_id = None

    threading.Thread(target=worker, name=scan_id, daemon=True).start()


def run_due_schedule(now=None) -> str | None:
    """Queue each profile's due slot and run pending jobs one at a time."""
    profiles = {row["id"]: row for row in list_account_profiles()}
    for account in profiles.values():
        if slot := due_slot(account, now):
            for item in account.get("watch_items") or []:
                claim_slot(watch_slot_key(slot, item["term"]),
                           account_id=account["id"], watch_item=item)
    return start_next_pending(profiles)


def start_next_pending(profiles: dict[str, dict] | None = None) -> str | None:
    profiles = profiles or {row["id"]: row for row in list_account_profiles()}
    if active_scan_id:
        return None
    for pending in pending_slots():
        account_id = pending["account_id"]
        slot = pending["slot_key"]
        account = profiles.get(account_id)
        if account is None:
            release_slot(slot, account_id=account_id)
            continue
        try:
            item = pending.get("watch_item")
            manifest = start_scan({
                "zip_code": item["zip_code"] if item else account["zip_code"],
                "radius_miles": item["radius_miles"] if item else account["radius_miles"],
                "keywords": [item["term"]] if item else account["terms"],
                "watch_item": item, "mode": "scheduled",
            }, account_id=account_id)
            finish_slot(slot, manifest["id"], account_id=account_id)
            return manifest["id"]
        except RuntimeError:
            return None
        except Exception:
            release_slot(slot, account_id=account_id)
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
                          account_id=account["id"])
    for item in items[1:]:
        claim_slot(watch_slot_key(batch, item["term"]),
                   account_id=account["id"], watch_item=item)
    return {"id": manifest["id"], "status": "running",
            "queued_count": len(items)}


def scheduler_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            run_due_schedule()
        except Exception as error:
            print(f"Scheduled scan did not start: {error}", flush=True)
        stop.wait(30)


def mark_interrupted_scans() -> None:
    for manifest in scan_manifests():
        if manifest.get("status") in {"queued", "running"}:
            manifest["status"] = "interrupted"
            manifest["stage"] = "Interrupted when the local server stopped"
            save_manifest(manifest)


class Handler(BaseHTTPRequestHandler):
    def _public_request(self) -> bool:
        path = unquote(urlsplit(self.path).path)
        return bool(PUBLIC_HOST and self.headers.get("Host", "").lower() == PUBLIC_HOST
                    and route_path(path)[1] == PUBLIC_PREFIX)

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
        if self._public_request() and path == "/api/searches":
            return self._error(403, "This page is available only on this computer")
        if self._public_request() and path in {"/admin", "/api/admin"} and not self._is_admin(identity):
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
                                    "active_scan_id": active_scan_id})
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
                                    "admin_emails": list_admin_emails()})
        if path == "/api/searches":
            return self._json(200, {"searches": list_searches()})
        if path == "/api/alerts":
            if email:
                try:
                    account = self._account(email)
                except ValueError as error:
                    return self._error(400, str(error))
                if not account["configured"]:
                    return self._json(200, {"unread_count": 0, "alerts": []})
                scan_ids = {scan["id"] for scan in account_scans(account)}
                return self._json(200, list_alerts(
                    search_id=account["search_id"], scan_ids=scan_ids))
            return self._json(200, {"unread_count": 0, "alerts": []}
                              if self._public_request() else list_alerts())
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
                relative = row.get("image_file")
                if isinstance(relative, str):
                    image = (source.parent / relative).resolve()
                    if image.is_relative_to(source.parent.resolve()) and image.is_file():
                        mime = mimetypes.guess_type(image.name)[0] or "image/jpeg"
                        return self._send(200, image.read_bytes(), mime)
                stored = get_image(ROOT / DEFAULT_DB, "facebook_marketplace",
                                   match.group(2))
                if stored:
                    return self._send(200, stored[1], stored[0])
                return self._error(404, "Image not found")
            except (FileNotFoundError, ValueError):
                return self._error(404, "Scan not found")
        if match := SCAN_ROUTE.fullmatch(path):
            if identity and not self._owns_scan(match.group(1), email):
                return self._error(404, "Scan not found")
            try:
                return self._json(200, public_scan(read_manifest(match.group(1))))
            except (FileNotFoundError, ValueError):
                return self._error(404, "Scan not found")
        static = {
            "/": "index.html", "/setup": "index.html",
            "/admin": "index.html",
            "/index.html": "index.html",
            "/styles.css": "styles.css", "/app.js": "app.js",
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
        self._send(200, body, mime, cache="no-cache")

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
                        "/api/account/scan"} and not (
                SEARCH_SCAN_ROUTE.fullmatch(path) or ALERT_READ_ROUTE.fullmatch(path)):
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
    mark_interrupted_scans()
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
