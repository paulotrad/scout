"""Saved local searches and deduplicated in-app alerts for completed scans."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, time, timedelta, timezone
import json
import hashlib
import math
from pathlib import Path
import re
import sqlite3
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from marketplace_db import DEFAULT_DB
from marketplace_scan_runner import ROOT, validate_settings


ALERT_DB = ROOT / DEFAULT_DB
GRADES = {"needs_repair": 1, "fair": 2, "good": 3, "like_new": 4, "new": 5}
QUERY_TERM = re.compile(r"^[^\x00-\x1f]{1,80}$")
EMAIL = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}$", re.I)
DEMO_ACCOUNT = "demo"
MAX_PRICE = 1_000_000
LOCK_FIELDS = ("zip_code", "radius_miles", "min_price", "max_price")
TIMEZONES = {
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Phoenix", "America/Los_Angeles", "America/Anchorage",
    "Pacific/Honolulu", "America/Puerto_Rico",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY,
    zip_code TEXT NOT NULL,
    radius_miles INTEGER NOT NULL,
    scan_keywords_json TEXT NOT NULL,
    item_terms_json TEXT NOT NULL,
    minimum_grade TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match_alerts (
    id INTEGER PRIMARY KEY,
    search_id INTEGER NOT NULL REFERENCES saved_searches(id) ON DELETE CASCADE,
    scan_id TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    title TEXT NOT NULL,
    item_type TEXT NOT NULL,
    condition_grade TEXT NOT NULL,
    matched_at TEXT NOT NULL,
    read_at TEXT,
    UNIQUE(search_id, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_match_alerts_recent
    ON match_alerts (read_at, matched_at DESC);
CREATE TABLE IF NOT EXISTS account_profiles (
    id TEXT PRIMARY KEY,
    email TEXT,
    search_id INTEGER NOT NULL REFERENCES saved_searches(id) ON DELETE CASCADE,
    plan TEXT NOT NULL,
    timezone TEXT NOT NULL,
    email_notifications_enabled INTEGER NOT NULL DEFAULT 1 CHECK(email_notifications_enabled IN (0, 1)),
    configured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_roles (
    email TEXT PRIMARY KEY COLLATE NOCASE,
    role TEXT NOT NULL CHECK(role = 'admin'),
    granted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS email_batches (
    account_id TEXT NOT NULL,
    batch_key TEXT NOT NULL,
    recipient TEXT NOT NULL,
    expected_scans INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    PRIMARY KEY (account_id, batch_key)
);
CREATE TABLE IF NOT EXISTS email_batch_scans (
    account_id TEXT NOT NULL,
    batch_key TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (account_id, batch_key, scan_id),
    FOREIGN KEY (account_id, batch_key)
        REFERENCES email_batches(account_id, batch_key) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS email_outbox (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    batch_key TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    sent_at TEXT,
    UNIQUE(account_id, batch_key)
);
CREATE TABLE IF NOT EXISTS scheduled_slots (
    account_id TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    scan_id TEXT,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (account_id, slot_key)
);
"""


def _connect(db_path: Path = ALERT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(saved_searches)")}
    for column in ("min_price", "max_price"):
        if column not in columns:
            default = "0" if column == "min_price" else "1000000"
            db.execute(f"ALTER TABLE saved_searches ADD COLUMN {column} REAL NOT NULL DEFAULT {default}")
    for column, default in (("watch_items_json", "'[]'"),
                            ("global_locks_json", "'{}'")):
        if column not in columns:
            db.execute(f"ALTER TABLE saved_searches ADD COLUMN {column} TEXT NOT NULL DEFAULT {default}")
    slot_columns = {row[1] for row in db.execute("PRAGMA table_info(scheduled_slots)")}
    if "watch_item_json" not in slot_columns:
        db.execute("ALTER TABLE scheduled_slots ADD COLUMN watch_item_json TEXT")
    profile_columns = {row[1] for row in db.execute("PRAGMA table_info(account_profiles)")}
    if "email" not in profile_columns:
        db.execute("ALTER TABLE account_profiles ADD COLUMN email TEXT")
    if "email_notifications_enabled" not in profile_columns:
        db.execute("ALTER TABLE account_profiles ADD COLUMN email_notifications_enabled "
                   "INTEGER NOT NULL DEFAULT 1 CHECK(email_notifications_enabled IN (0, 1))")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_account_profiles_email "
               "ON account_profiles(email COLLATE NOCASE)")
    db.commit()
    return db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_search(payload: dict) -> dict:
    zip_code, radius, keywords = validate_settings(
        payload.get("zip_code"), payload.get("radius_miles"),
        payload.get("scan_keywords", ["free"]))
    terms = payload.get("item_terms", [])
    if not isinstance(terms, list) or len(terms) > 25:
        raise ValueError("Enter up to 25 item terms")
    cleaned = []
    for term in terms:
        if not isinstance(term, str) or not QUERY_TERM.fullmatch(term.strip()):
            raise ValueError("Each item term must be 1–80 printable characters")
        term = term.strip()
        if term.casefold() not in {saved.casefold() for saved in cleaned}:
            cleaned.append(term)
    grade = payload.get("minimum_grade", "any")
    if grade not in {"any", *GRADES}:
        raise ValueError("Choose a valid minimum condition")
    minimum_price = payload.get("min_price", 0)
    maximum_price = payload.get("max_price", MAX_PRICE)
    if (isinstance(minimum_price, bool) or isinstance(maximum_price, bool) or
            not isinstance(minimum_price, (int, float)) or
            not isinstance(maximum_price, (int, float)) or
            not math.isfinite(minimum_price) or not math.isfinite(maximum_price) or
            minimum_price < 0 or maximum_price > MAX_PRICE or
            minimum_price > maximum_price):
        raise ValueError("Choose a price range from $0 to $1,000,000")
    return {"zip_code": zip_code, "radius_miles": radius,
            "scan_keywords": keywords, "item_terms": cleaned,
            "minimum_grade": grade,
            "min_price": round(minimum_price, 2),
            "max_price": round(maximum_price, 2)}


def _legacy_watch_items(search: dict) -> list[dict]:
    values = {"zip_code": search["zip_code"],
              "radius_miles": search["radius_miles"],
              "min_price": search.get("min_price", 0),
              "max_price": search.get("max_price", MAX_PRICE)}
    return [{"term": term, **values} for term in
            (search.get("item_terms") or search.get("scan_keywords") or [])]


def _validated_watch_items(payload: dict, search: dict) -> tuple[list[dict], dict]:
    locks = payload.get("global_locks", {key: True for key in LOCK_FIELDS})
    if not isinstance(locks, dict) or any(type(locks.get(key)) is not bool
                                         for key in LOCK_FIELDS):
        raise ValueError("Choose which global settings are locked")
    locks = {key: locks[key] for key in LOCK_FIELDS}
    raw = payload.get("watch_items")
    if raw is None:
        return _legacy_watch_items(search), locks
    if not isinstance(raw, list) or not 1 <= len(raw) <= 25:
        raise ValueError("Enter one to 25 watchlist items")
    items, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("Each watchlist item needs a keyword and settings")
        term = entry.get("term")
        if not isinstance(term, str) or not QUERY_TERM.fullmatch(term.strip()):
            raise ValueError("Each keyword must be 1–80 printable characters")
        term = term.strip()
        if term.casefold() in seen:
            raise ValueError("Each watchlist keyword must be unique")
        seen.add(term.casefold())
        resolved = {key: search[key] if locks[key] else entry.get(key)
                    for key in LOCK_FIELDS}
        validated = validate_search({**resolved, "scan_keywords": [term],
                                     "item_terms": [term]})
        items.append({"term": term, **{key: validated[key] for key in LOCK_FIELDS}})
    return items, locks


def _search(row: sqlite3.Row) -> dict:
    result = {"id": row["id"], "zip_code": row["zip_code"],
            "radius_miles": row["radius_miles"],
            "scan_keywords": json.loads(row["scan_keywords_json"]),
            "item_terms": json.loads(row["item_terms_json"]),
            "minimum_grade": row["minimum_grade"],
            "min_price": row["min_price"], "max_price": row["max_price"],
            "created_at": row["created_at"]}
    result["watch_items"] = json.loads(row["watch_items_json"]) or _legacy_watch_items(result)
    result["global_locks"] = {**{key: True for key in LOCK_FIELDS},
                              **json.loads(row["global_locks_json"])}
    return result


def create_search(payload: dict, db_path: Path = ALERT_DB) -> dict:
    settings = validate_search(payload)
    with closing(_connect(db_path)) as db, db:
        cursor = db.execute("""
            INSERT INTO saved_searches
                (zip_code, radius_miles, scan_keywords_json, item_terms_json,
                 minimum_grade, min_price, max_price, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (settings["zip_code"], settings["radius_miles"],
              json.dumps(settings["scan_keywords"]),
              json.dumps(settings["item_terms"]),
              settings["minimum_grade"], settings["min_price"],
              settings["max_price"], _now()))
        settings["id"] = cursor.lastrowid
    return settings


def list_searches(db_path: Path = ALERT_DB) -> list[dict]:
    with closing(_connect(db_path)) as db:
        rows = db.execute("SELECT * FROM saved_searches ORDER BY id DESC").fetchall()
    return [_search(row) for row in rows]


def get_search(search_id: int, db_path: Path = ALERT_DB) -> dict | None:
    with closing(_connect(db_path)) as db:
        row = db.execute("SELECT * FROM saved_searches WHERE id = ?",
                         (search_id,)).fetchone()
    return _search(row) if row else None


def delete_search(search_id: int, db_path: Path = ALERT_DB) -> bool:
    with closing(_connect(db_path)) as db, db:
        cursor = db.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
        return cursor.rowcount > 0


def item_matches(search: dict, scan: dict, item: dict) -> bool:
    """Match only reviewed physical items in the search's measured area."""
    if item.get("listing_kind") != "product":
        return False
    distance = item.get("distance_miles_approx")
    if (isinstance(distance, bool) or not isinstance(distance, (int, float)) or
            not math.isfinite(distance)):
        return False
    price = item.get("asking_price")
    if (isinstance(price, bool) or not isinstance(price, (int, float)) or
            not math.isfinite(price) or
            ("zero_price_card_may_not_be_free" in (item.get("review_flags") or []) and
             item.get("price_source") != "seller_description")):
        return False
    grade = item.get("condition_grade") or "unknown"
    minimum = search["minimum_grade"]
    if minimum != "any" and GRADES.get(grade, 0) < GRADES[minimum]:
        return False
    haystack = " ".join(str(item.get(key) or "") for key in (
        "title", "item_type", "category", "identity", "description",
        "refactored_description")).casefold()
    watch_items = search.get("watch_items") or _legacy_watch_items(search)
    scan_term = scan.get("watch_term")
    return any(
        config["zip_code"] == scan.get("zip_code") and
        (scan.get("radius_miles") or 0) >= config["radius_miles"] and
        distance <= config["radius_miles"] and
        config["min_price"] <= price <= config["max_price"] and
        (not scan_term or config["term"].casefold() == scan_term.casefold()) and
        (price == 0 if config["term"].casefold() == "free"
         else config["term"].casefold() in haystack)
        for config in watch_items)


def validate_account(payload: dict) -> dict:
    terms = payload.get("terms")
    if terms is None and isinstance(payload.get("watch_items"), list):
        terms = [item.get("term") for item in payload["watch_items"]
                 if isinstance(item, dict)]
    if not isinstance(terms, list) or not 1 <= len(terms) <= 25:
        raise ValueError("Enter one to 25 items or keywords")
    search = validate_search({
        "zip_code": payload.get("zip_code"),
        "radius_miles": payload.get("radius_miles"),
        "scan_keywords": terms, "item_terms": terms,
        "minimum_grade": payload.get("minimum_grade", "any"),
        "min_price": payload.get("min_price", 0),
        "max_price": payload.get("max_price", MAX_PRICE),
    })
    plan = payload.get("plan", "free")
    if plan not in {"free", "plus_preview"}:
        raise ValueError("Choose Free or Plus preview")
    email_notifications_enabled = payload.get("email_notifications_enabled", True)
    if not isinstance(email_notifications_enabled, bool):
        raise ValueError("Choose whether to receive email notifications")
    zone = payload.get("timezone", "America/New_York")
    if zone not in TIMEZONES:
        raise ValueError("Choose a supported US timezone")
    try:
        ZoneInfo(zone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("This timezone is unavailable on this computer") from error
    watch_items, locks = _validated_watch_items(payload, search)
    if [item["term"].casefold() for item in watch_items] != [
            term.casefold() for term in search["scan_keywords"]]:
        raise ValueError("Watchlist keywords must match the saved terms")
    return {**search, "terms": search["scan_keywords"],
            "watch_items": watch_items, "global_locks": locks,
            "plan": plan, "timezone": zone,
            "email_notifications_enabled": email_notifications_enabled}


def normalize_email(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter an email address")
    email = value.strip().casefold()
    if len(email) > 254 or not EMAIL.fullmatch(email):
        raise ValueError("Enter a valid email address")
    return email


def is_admin_email(email: str, db_path: Path = ALERT_DB) -> bool:
    """Only a verified Access email should be passed to this role lookup."""
    email = normalize_email(email)
    with closing(_connect(db_path)) as db:
        return db.execute("SELECT 1 FROM account_roles WHERE email = ? AND role = 'admin'",
                          (email,)).fetchone() is not None


def grant_admin(email: str, db_path: Path = ALERT_DB) -> None:
    email = normalize_email(email)
    with closing(_connect(db_path)) as db, db:
        db.execute("INSERT OR IGNORE INTO account_roles (email, role, granted_at) "
                   "VALUES (?, 'admin', ?)", (email, _now()))


def revoke_admin(email: str, db_path: Path = ALERT_DB) -> bool:
    email = normalize_email(email)
    with closing(_connect(db_path)) as db, db:
        return db.execute("DELETE FROM account_roles WHERE email = ? AND role = 'admin'",
                          (email,)).rowcount > 0


def list_admin_emails(db_path: Path = ALERT_DB) -> list[str]:
    with closing(_connect(db_path)) as db:
        return [row[0] for row in db.execute(
            "SELECT email FROM account_roles WHERE role = 'admin' ORDER BY email")]


def _default_account(email: str | None = None) -> dict:
    account = {"configured": False, "id": None, "email": email,
            "display_name": "Demo account", "zip_code": "21009",
            "radius_miles": 15, "terms": ["free"],
            "minimum_grade": "any", "min_price": 0, "max_price": 1000,
            "plan": "free", "timezone": "America/New_York",
            "email_notifications_enabled": True,
            "next_run_at": None, "account_schema_version": 3}
    account["watch_items"] = _legacy_watch_items({**account, "scan_keywords": account["terms"]})
    account["global_locks"] = {key: True for key in LOCK_FIELDS}
    return account


def _account_from_row(row: sqlite3.Row) -> dict:
    search = _search(row)
    account = {"configured": True, "id": row["account_id"],
               "email": row["email"], "display_name": "Demo account",
               "search_id": search["id"], "zip_code": search["zip_code"],
               "radius_miles": search["radius_miles"],
               "terms": search["scan_keywords"],
               "minimum_grade": search["minimum_grade"],
               "min_price": search["min_price"], "max_price": search["max_price"],
               "watch_items": search["watch_items"],
               "global_locks": search["global_locks"],
               "plan": row["plan"], "timezone": row["timezone"],
               "email_notifications_enabled": bool(row["email_notifications_enabled"]),
               "configured_at": row["configured_at"],
               "account_schema_version": 3}
    account["next_run_at"] = next_scheduled_at(account).isoformat()
    return account


def get_account(email: str | None = None, db_path: Path = ALERT_DB,
                *, allow_legacy_claim: bool = True) -> dict:
    if email is not None:
        email = normalize_email(email)
    with closing(_connect(db_path)) as db:
        if email is None:
            row = db.execute("""
                SELECT p.id AS account_id, p.email, p.plan, p.timezone,
                       p.email_notifications_enabled,
                       p.configured_at, s.*
                FROM account_profiles p JOIN saved_searches s ON s.id = p.search_id
                WHERE p.id = ?
            """, (DEMO_ACCOUNT,)).fetchone()
        else:
            row = db.execute("""
                SELECT p.id AS account_id, p.email, p.plan, p.timezone,
                       p.email_notifications_enabled,
                       p.configured_at, s.*
                FROM account_profiles p JOIN saved_searches s ON s.id = p.search_id
                WHERE p.email = ? COLLATE NOCASE
            """, (email,)).fetchone()
    if row:
        return _account_from_row(row)
    if email is not None and allow_legacy_claim:
        legacy = get_account(None, db_path)
        if legacy["configured"] and legacy["email"] is None:
            return {**legacy, "configured": False, "id": None, "email": email,
                    "search_id": None, "next_run_at": None,
                    "claimable_legacy": True}
    return _default_account(email)


def open_account(email: str, db_path: Path = ALERT_DB,
                 *, allow_legacy_claim: bool = True) -> dict:
    """Load an email profile, creating and saving its initial plan if needed."""
    account = get_account(email, db_path,
                          allow_legacy_claim=allow_legacy_claim)
    if account["configured"]:
        return account
    return save_account(account, db_path,
                        allow_legacy_claim=allow_legacy_claim)


def save_account(payload: dict, db_path: Path = ALERT_DB,
                 *, allow_legacy_claim: bool = True) -> dict:
    """Create or update an email-labelled local profile without verification."""
    email = normalize_email(payload.get("email"))
    profile = validate_account(payload)
    values = (profile["zip_code"], profile["radius_miles"],
              json.dumps(profile["terms"]), json.dumps(profile["terms"]),
              profile["minimum_grade"], profile["min_price"],
              profile["max_price"], json.dumps(profile["watch_items"]),
              json.dumps(profile["global_locks"]))
    with closing(_connect(db_path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT id, search_id, email_notifications_enabled FROM account_profiles "
                              "WHERE email = ? COLLATE NOCASE", (email,)).fetchone()
        if existing is None and allow_legacy_claim:
            existing = db.execute("SELECT id, search_id, email_notifications_enabled FROM account_profiles "
                                  "WHERE id = ? AND email IS NULL",
                                  (DEMO_ACCOUNT,)).fetchone()
        notifications_enabled = (profile["email_notifications_enabled"]
                                 if "email_notifications_enabled" in payload or not existing
                                 else bool(existing["email_notifications_enabled"]))
        if existing:
            db.execute("""
                UPDATE saved_searches SET zip_code = ?, radius_miles = ?,
                    scan_keywords_json = ?, item_terms_json = ?,
                    minimum_grade = ?, min_price = ?, max_price = ?,
                    watch_items_json = ?, global_locks_json = ? WHERE id = ?
            """, (*values, existing["search_id"]))
            db.execute("""
                UPDATE account_profiles SET email = ?, plan = ?, timezone = ?,
                    email_notifications_enabled = ?
                WHERE id = ?
            """, (email, profile["plan"], profile["timezone"],
                  int(notifications_enabled), existing["id"]))
            if not notifications_enabled:
                db.execute("DELETE FROM email_outbox WHERE account_id = ? AND sent_at IS NULL",
                           (existing["id"],))
                db.execute("UPDATE email_batches SET closed_at = COALESCE(closed_at, ?) "
                           "WHERE account_id = ?", (_now(), existing["id"]))
        else:
            cursor = db.execute("""
                INSERT INTO saved_searches
                    (zip_code, radius_miles, scan_keywords_json, item_terms_json,
                     minimum_grade, min_price, max_price, watch_items_json,
                     global_locks_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (*values, _now()))
            db.execute("""
                INSERT INTO account_profiles
                    (id, email, search_id, plan, timezone,
                     email_notifications_enabled, configured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ("acct-" + uuid.uuid4().hex, email, cursor.lastrowid,
                  profile["plan"], profile["timezone"],
                  int(notifications_enabled), _now()))
    return get_account(email, db_path)


def save_demo_account(payload: dict, db_path: Path = ALERT_DB) -> dict:
    """Instant local demo account: one watchlist, no payment entitlement."""
    profile = validate_account(payload)
    with closing(_connect(db_path)) as db, db:
        existing = db.execute(
            "SELECT search_id, email_notifications_enabled FROM account_profiles WHERE id = ?",
            (DEMO_ACCOUNT,)).fetchone()
        notifications_enabled = (profile["email_notifications_enabled"]
                                 if "email_notifications_enabled" in payload or not existing
                                 else bool(existing["email_notifications_enabled"]))
        values = (profile["zip_code"], profile["radius_miles"],
                  json.dumps(profile["terms"]), json.dumps(profile["terms"]),
                  profile["minimum_grade"], profile["min_price"],
                  profile["max_price"], json.dumps(profile["watch_items"]),
                  json.dumps(profile["global_locks"]))
        if existing:
            search_id = existing["search_id"]
            db.execute("""
                UPDATE saved_searches SET zip_code = ?, radius_miles = ?,
                    scan_keywords_json = ?, item_terms_json = ?,
                    minimum_grade = ?, min_price = ?, max_price = ?,
                    watch_items_json = ?, global_locks_json = ? WHERE id = ?
            """, (*values, search_id))
            db.execute("""
                UPDATE account_profiles SET plan = ?, timezone = ?,
                    email_notifications_enabled = ? WHERE id = ?
            """, (profile["plan"], profile["timezone"],
                  int(notifications_enabled), DEMO_ACCOUNT))
            if not notifications_enabled:
                db.execute("DELETE FROM email_outbox WHERE account_id = ? AND sent_at IS NULL",
                           (DEMO_ACCOUNT,))
                db.execute("UPDATE email_batches SET closed_at = COALESCE(closed_at, ?) "
                           "WHERE account_id = ?", (_now(), DEMO_ACCOUNT))
        else:
            cursor = db.execute("""
                INSERT INTO saved_searches
                    (zip_code, radius_miles, scan_keywords_json, item_terms_json,
                     minimum_grade, min_price, max_price, watch_items_json,
                     global_locks_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (*values, _now()))
            search_id = cursor.lastrowid
            db.execute("""
                INSERT INTO account_profiles
                    (id, search_id, plan, timezone,
                     email_notifications_enabled, configured_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (DEMO_ACCOUNT, search_id, profile["plan"], profile["timezone"],
                  int(notifications_enabled), _now()))
    return get_demo_account(db_path)


def get_demo_account(db_path: Path = ALERT_DB) -> dict:
    return get_account(None, db_path)


def list_account_profiles(db_path: Path = ALERT_DB) -> list[dict]:
    """Return configured local profiles for the admin view."""
    with closing(_connect(db_path)) as db:
        rows = db.execute("""
            SELECT p.id AS account_id, p.email, p.plan, p.timezone,
                   p.email_notifications_enabled, p.configured_at, s.*
            FROM account_profiles p JOIN saved_searches s ON s.id = p.search_id
            ORDER BY p.configured_at DESC, p.id
        """).fetchall()
    profiles = []
    for row in rows:
        search = _search(row)
        profile = {
            "id": row["account_id"],
            "display_name": "Demo account" if row["account_id"] == DEMO_ACCOUNT else "Local account",
            "email": row["email"], "configured": True,
            "plan": row["plan"],
            "email_notifications_enabled": bool(row["email_notifications_enabled"]),
            "subscription_status": "preview" if row["plan"] == "plus_preview" else "free",
            "timezone": row["timezone"],
            "configured_at": row["configured_at"],
            "search_id": search["id"],
            "zip_code": search["zip_code"],
            "radius_miles": search["radius_miles"],
            "terms": search["scan_keywords"],
            "watch_items": search["watch_items"],
            "global_locks": search["global_locks"],
            "min_price": search["min_price"],
            "max_price": search["max_price"],
            "minimum_grade": search["minimum_grade"],
        }
        profile["next_run_at"] = next_scheduled_at(profile).isoformat()
        profiles.append(profile)
    return profiles


def scheduled_scan_owners(db_path: Path = ALERT_DB) -> dict[str, str]:
    with closing(_connect(db_path)) as db:
        rows = db.execute("""
            SELECT scan_id, account_id FROM scheduled_slots WHERE scan_id IS NOT NULL
        """).fetchall()
    return {row["scan_id"]: row["account_id"] for row in rows}


def account_id_for_search(search_id: int, db_path: Path = ALERT_DB) -> str | None:
    with closing(_connect(db_path)) as db:
        row = db.execute("SELECT id FROM account_profiles WHERE search_id = ?",
                         (search_id,)).fetchone()
    return row["id"] if row else None


def _scheduled_hours(account: dict) -> tuple[int, ...]:
    return (7, 12) if account["plan"] == "plus_preview" else (7,)


def next_scheduled_at(account: dict, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(account["timezone"])
    local = now.astimezone(zone)
    for day_offset in (0, 1):
        day = local.date() + timedelta(days=day_offset)
        for hour in _scheduled_hours(account):
            slot = datetime.combine(day, time(hour), tzinfo=zone)
            if slot > local:
                return slot
    raise AssertionError("The next daily scan should always be within one day")


def due_slot(account: dict, now: datetime | None = None) -> str | None:
    """Allow a run during its scheduled hour; missed hours stay missed."""
    if not account.get("configured"):
        return None
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(account["timezone"]))
    if local.hour not in _scheduled_hours(account):
        return None
    return f"{local.date().isoformat()}-{local.hour:02d}"


def watch_slot_key(slot: str, term: str) -> str:
    digest = hashlib.sha256(term.casefold().encode("utf-8")).hexdigest()[:16]
    return f"{slot}:item:{digest}"


def claim_slot(slot_key: str, db_path: Path = ALERT_DB,
               account_id: str = DEMO_ACCOUNT,
               watch_item: dict | None = None) -> bool:
    with closing(_connect(db_path)) as db, db:
        cursor = db.execute("""
            INSERT OR IGNORE INTO scheduled_slots
                (account_id, slot_key, claimed_at, watch_item_json)
            VALUES (?, ?, ?, ?)
        """, (account_id, slot_key, _now(),
              json.dumps(watch_item) if watch_item else None))
        return cursor.rowcount > 0


def pending_slots(db_path: Path = ALERT_DB) -> list[dict]:
    with closing(_connect(db_path)) as db:
        rows = db.execute("""
            SELECT account_id, slot_key, claimed_at, watch_item_json
            FROM scheduled_slots WHERE scan_id IS NULL
            ORDER BY claimed_at, account_id
        """).fetchall()
    return [{"account_id": row["account_id"], "slot_key": row["slot_key"],
             "claimed_at": row["claimed_at"],
             **({"watch_item": json.loads(row["watch_item_json"])}
                if row["watch_item_json"] else {})} for row in rows]


def finish_slot(slot_key: str, scan_id: str, db_path: Path = ALERT_DB,
                account_id: str = DEMO_ACCOUNT) -> None:
    with closing(_connect(db_path)) as db, db:
        db.execute("""
            UPDATE scheduled_slots SET scan_id = ?
            WHERE account_id = ? AND slot_key = ?
        """, (scan_id, account_id, slot_key))


def release_slot(slot_key: str, db_path: Path = ALERT_DB,
                 account_id: str = DEMO_ACCOUNT) -> None:
    with closing(_connect(db_path)) as db, db:
        db.execute("""
            DELETE FROM scheduled_slots
            WHERE account_id = ? AND slot_key = ? AND scan_id IS NULL
        """, (account_id, slot_key))


def record_scan_matches(scan: dict, db_path: Path = ALERT_DB) -> int:
    """A listing creates one alert per saved search, even across repeat scans."""
    if scan.get("status") != "complete":
        return 0
    account_id = scan.get("account_id")
    if account_id:
        with closing(_connect(db_path)) as db:
            owner = db.execute("SELECT search_id FROM account_profiles WHERE id = ?",
                               (account_id,)).fetchone()
        search = get_search(owner["search_id"], db_path) if owner else None
        searches = [search] if search else []
    else:
        searches = list_searches(db_path)
    added = 0
    with closing(_connect(db_path)) as db, db:
        for search in searches:
            for item in scan.get("listings") or []:
                if not item_matches(search, scan, item):
                    continue
                cursor = db.execute("""
                    INSERT OR IGNORE INTO match_alerts
                        (search_id, scan_id, listing_id, title, item_type,
                         condition_grade, matched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (search["id"], scan["id"], str(item["id"]),
                      str(item.get("title") or ""),
                      str(item.get("item_type") or ""),
                      str(item.get("condition_grade") or "unknown"), _now()))
                added += cursor.rowcount
    return added


def list_alerts(db_path: Path = ALERT_DB, limit: int = 100,
                search_id: int | None = None,
                scan_ids: set[str] | None = None) -> dict:
    with closing(_connect(db_path)) as db:
        restriction = " AND search_id = ?" if search_id is not None else ""
        scan_restriction = (" AND scan_id IN (SELECT value FROM json_each(?))"
                            if scan_ids is not None else "")
        scan_arguments = (json.dumps(sorted(scan_ids)),) if scan_ids is not None else ()
        arguments = ((search_id,) if search_id is not None else ()) + scan_arguments
        unread = db.execute(
            "SELECT COUNT(*) FROM match_alerts WHERE read_at IS NULL" +
            restriction + scan_restriction,
            arguments).fetchone()[0]
        rows = db.execute("""
            SELECT a.*, s.zip_code, s.item_terms_json
            FROM match_alerts a JOIN saved_searches s ON s.id = a.search_id
            WHERE (? IS NULL OR a.search_id = ?)
        """ + scan_restriction + " ORDER BY a.matched_at DESC, a.id DESC LIMIT ?",
                          (search_id, search_id, *scan_arguments, limit)).fetchall()
    return {"unread_count": unread, "alerts": [
        {"id": row["id"], "search_id": row["search_id"],
         "scan_id": row["scan_id"], "listing_id": row["listing_id"],
         "title": row["title"], "item_type": row["item_type"],
         "condition_grade": row["condition_grade"],
         "zip_code": row["zip_code"],
         "item_terms": json.loads(row["item_terms_json"]),
         "matched_at": row["matched_at"], "read_at": row["read_at"]}
        for row in rows]}


def mark_read(alert_id: int, db_path: Path = ALERT_DB,
              search_id: int | None = None) -> bool:
    with closing(_connect(db_path)) as db, db:
        cursor = db.execute(
            "UPDATE match_alerts SET read_at = COALESCE(read_at, ?) "
            "WHERE id = ? AND (? IS NULL OR search_id = ?)",
            (_now(), alert_id, search_id, search_id))
        return cursor.rowcount > 0
