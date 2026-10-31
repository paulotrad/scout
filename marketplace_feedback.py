"""Per-account feedback on Scout's item identification and analysis."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import sqlite3

from marketplace_alerts import ALERT_DB

ISSUES = {"wrong_item", "wrong_condition", "wrong_price", "service_or_ad", "other"}
GRADES = {"new", "like_new", "good", "fair", "poor", "needs_repair", "unknown"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_feedback (
    account_id TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    vote TEXT NOT NULL CHECK(vote IN ('up', 'down')),
    issue TEXT NOT NULL DEFAULT '',
    corrected_item_type TEXT NOT NULL DEFAULT '',
    corrected_condition TEXT NOT NULL DEFAULT '',
    corrected_price REAL,
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_item_feedback_scan
    ON item_feedback (account_id, scan_id);
"""


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(item_feedback)")}
    for name, definition in (
            ("corrected_item_type", "TEXT NOT NULL DEFAULT ''"),
            ("corrected_condition", "TEXT NOT NULL DEFAULT ''"),
            ("corrected_price", "REAL"),
            ("note", "TEXT NOT NULL DEFAULT ''")):
        if name not in columns:
            db.execute(f"ALTER TABLE item_feedback ADD COLUMN {name} {definition}")


def _record(row: tuple) -> dict:
    vote, issue, item_type, condition, price, note, updated_at = row
    return {"vote": vote, "issue": issue,
            "corrected_item_type": item_type or "",
            "corrected_condition": condition or "",
            "corrected_price": price, "note": note or "",
            "updated_at": updated_at}


def feedback_for_listings(account_id: str, listing_ids: list[str],
                          db_path: Path = ALERT_DB) -> dict[str, dict]:
    """Show a person's vote wherever the same Marketplace listing reappears."""
    if not listing_ids:
        return {}
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path, timeout=5)) as db:
        _ensure_schema(db)
        result = {}
        for offset in range(0, len(listing_ids), 500):
            group = listing_ids[offset:offset + 500]
            rows = db.execute(
                "SELECT listing_id, vote, issue, corrected_item_type, "
                "corrected_condition, corrected_price, note, updated_at FROM item_feedback "
                "WHERE account_id = ? AND listing_id IN (" +
                ",".join("?" for _ in group) + ")", (account_id, *group))
            result.update({row[0]: _record(row[1:]) for row in rows})
        return result


def save_feedback(account_id: str, scan_id: str, listing_id: str,
                  vote: str, issue: str = "", db_path: Path = ALERT_DB,
                  *, correction: dict | None = None) -> dict:
    if vote not in {"up", "down"} or issue not in ISSUES | {""}:
        raise ValueError("Choose a valid feedback option")
    if correction is not None and not isinstance(correction, dict):
        raise ValueError("Enter a valid correction")
    correction = correction or {}
    item_type = str(correction.get("corrected_item_type") or "").strip()
    condition = str(correction.get("corrected_condition") or "").strip()
    raw_price = correction.get("corrected_price")
    note = str(correction.get("note") or "").strip()
    if len(item_type) > 80 or len(note) > 300 or condition not in GRADES | {""}:
        raise ValueError("Correction is too long or has an invalid condition")
    price = None
    if raw_price not in (None, ""):
        try:
            price = float(raw_price)
        except (TypeError, ValueError) as error:
            raise ValueError("Enter a valid price") from error
        if not math.isfinite(price) or not 0 <= price <= 1_000_000:
            raise ValueError("Enter a price between $0 and $1,000,000")
    if vote == "up":
        issue = ""
    if vote != "down" or issue != "wrong_item":
        item_type = ""
    if vote != "down" or issue != "wrong_condition":
        condition = ""
    if vote != "down" or issue != "wrong_price":
        price = None
    if vote != "down":
        note = ""
    stamp = datetime.now(timezone.utc).isoformat()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path, timeout=5)) as db, db:
        _ensure_schema(db)
        db.execute(
            "INSERT INTO item_feedback (account_id, listing_id, scan_id, vote, issue, "
            "corrected_item_type, corrected_condition, corrected_price, note, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, listing_id) DO UPDATE SET "
            "scan_id = excluded.scan_id, vote = excluded.vote, "
            "issue = excluded.issue, corrected_item_type = excluded.corrected_item_type, "
            "corrected_condition = excluded.corrected_condition, "
            "corrected_price = excluded.corrected_price, note = excluded.note, "
            "updated_at = excluded.updated_at",
            (account_id, listing_id, scan_id, vote, issue, item_type,
             condition, price, note, stamp))
    return _record((vote, issue, item_type, condition, price, note, stamp))


def corrections_to_review(limit: int = 50, db_path: Path = ALERT_DB) -> list[dict]:
    """Keep user corrections visible to a human; they never change model output."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path, timeout=5)) as db:
        _ensure_schema(db)
        rows = db.execute(
            "SELECT account_id, scan_id, listing_id, vote, issue, "
            "corrected_item_type, corrected_condition, corrected_price, note, updated_at "
            "FROM item_feedback WHERE vote = 'down' ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(limit, 200)),))
        return [{"account_id": row[0], "scan_id": row[1], "listing_id": row[2],
                 **_record(row[3:])} for row in rows]


def correction_count(db_path: Path = ALERT_DB) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path, timeout=5)) as db:
        _ensure_schema(db)
        return int(db.execute(
            "SELECT COUNT(*) FROM item_feedback WHERE vote = 'down'").fetchone()[0])


def feedback_metrics(db_path: Path = ALERT_DB) -> dict:
    """Summarize current reviewer votes without treating them as ground truth."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path, timeout=5)) as db:
        _ensure_schema(db)
        has_listings = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'listings'"
        ).fetchone() is not None
        if has_listings:
            rows = db.execute("""
                SELECT f.listing_id, f.vote, f.issue, l.vision_json
                FROM item_feedback f LEFT JOIN listings l
                  ON l.source = 'facebook_marketplace'
                 AND l.listing_id = f.listing_id
            """).fetchall()
        else:
            rows = db.execute(
                "SELECT listing_id, vote, issue, NULL FROM item_feedback"
            ).fetchall()
    categories: dict[str, dict] = {}
    issues = {issue: 0 for issue in sorted(ISSUES)}
    for _, vote, issue, vision_json in rows:
        try:
            observations = (json.loads(vision_json or "{}")
                            .get("observations") or {})
        except (ValueError, TypeError, AttributeError):
            observations = {}
        category = str(observations.get("category") or "uncategorized")[:80]
        bucket = categories.setdefault(category, {"category": category,
                                                   "reviewed": 0,
                                                   "confirmed": 0,
                                                   "corrections": 0,
                                                   "issues": {}})
        bucket["reviewed"] += 1
        bucket["confirmed" if vote == "up" else "corrections"] += 1
        if vote == "down":
            issue_name = issue if issue in issues else "other"
            issues[issue_name] += 1
            bucket["issues"][issue_name] = bucket["issues"].get(issue_name, 0) + 1
    reviewed = len(rows)
    return {"reviewed": reviewed,
            "unique_listings": len({row[0] for row in rows}),
            "confirmed": sum(row[1] == "up" for row in rows),
            "corrections": sum(row[1] == "down" for row in rows),
            "sample_sufficient": reviewed >= 20,
            "by_issue": [{"issue": key, "count": value}
                         for key, value in issues.items() if value],
            "by_category": sorted(categories.values(),
                                  key=lambda item: (-item["reviewed"],
                                                    item["category"]))}
