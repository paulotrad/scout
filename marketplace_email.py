"""Durable, match-only email digests for completed Scout scan batches."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import json
import os
import re
import smtplib
import ssl
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from marketplace_alerts import ALERT_DB, _connect, normalize_email


_send_lock = threading.Lock()
_BATCH_KEY = re.compile(r"[a-z0-9:-]{1,128}\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def prepare_email_batch(account_id: str, batch_key: str, expected_scans: int,
                        db_path: Path = ALERT_DB) -> bool:
    """Snapshot a profile's recipient before the first scan starts."""
    if not _BATCH_KEY.fullmatch(batch_key) or not 1 <= expected_scans <= 25:
        raise ValueError("Invalid email batch")
    with closing(_connect(db_path)) as db, db:
        profile = db.execute("SELECT email, email_notifications_enabled "
                             "FROM account_profiles WHERE id = ?",
                             (account_id,)).fetchone()
        if not profile or not profile["email"] or not profile["email_notifications_enabled"]:
            return False
        recipient = normalize_email(profile["email"])
        db.execute("""
            INSERT OR IGNORE INTO email_batches
                (account_id, batch_key, recipient, expected_scans, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (account_id, batch_key, recipient, expected_scans, _now()))
    return True


def _item_summary(item: dict) -> dict:
    return {
        "id": str(item.get("id") or "")[:80],
        "title": " ".join(str(item.get("display_title") or item.get("title") or
                              "Marketplace item").split())[:180],
        "price": item.get("asking_price") if isinstance(item.get("asking_price"),
                                                          (int, float)) else None,
        "condition": str(item.get("condition_grade") or "unknown")[:40],
        "location": " ".join(str(item.get("location") or "").split())[:100],
    }


def _public_base_url() -> str:
    host = os.environ.get("SCOUT_PUBLIC_HOST", "").strip().lower()
    if not re.fullmatch(r"[a-z0-9.-]+", host):
        return ""
    return f"https://{host}/Facebooknotify"


def _summary_text(rows: list[dict], base_url: str) -> tuple[str, str]:
    matches = [(row, item) for row in rows for item in row["items"]]
    subject = f"Scout found {len(matches)} new matching item{'s' if len(matches) != 1 else ''}"
    lines = [f"Scout found {len(matches)} new item{'s' if len(matches) != 1 else ''} "
             "matching your saved search criteria.", ""]
    for number, (row, item) in enumerate(matches, 1):
        price = item["price"]
        price_label = ("FREE" if price == 0 else f"${price:,.2f}"
                       if isinstance(price, (int, float)) else "Price unavailable")
        lines.append(f"{number}. {item['title']}")
        lines.append(f"   Search: {row['keyword']} | {price_label} | "
                     f"Condition: {item['condition'].replace('_', ' ')}")
        if item["location"]:
            lines.append(f"   Location: {item['location']}")
        lines.append(f"   View result: {base_url}/scan/{quote(row['scan_id'])}"
                     f"#item-{quote(item['id'])}")
        lines.append("")
    lines.extend(["These are listing details and AI estimates. Confirm price and condition "
                  "with the seller before making a decision.",
                  "You received this because your Scout watchlist found new matches."])
    return subject, "\n".join(lines) + "\n"


def finish_email_scan(scan: dict, db_path: Path = ALERT_DB) -> bool:
    """Record one finished scan and queue a digest once its whole batch is done."""
    account_id = scan.get("account_id")
    batch_key = scan.get("email_batch_key")
    scan_id = scan.get("id")
    if not account_id or not batch_key or not scan_id or scan.get("status") not in {
            "complete", "failed"}:
        return False
    with closing(_connect(db_path)) as db, db:
        batch = db.execute("""
            SELECT b.recipient, b.expected_scans, b.closed_at,
                   p.email_notifications_enabled
            FROM email_batches b JOIN account_profiles p ON p.id = b.account_id
            WHERE b.account_id = ? AND b.batch_key = ?
        """, (account_id, batch_key)).fetchone()
        if not batch or batch["closed_at"]:
            return False
        if not batch["email_notifications_enabled"]:
            db.execute("UPDATE email_batches SET closed_at = ? "
                       "WHERE account_id = ? AND batch_key = ?",
                       (_now(), account_id, batch_key))
            return False
        matched_ids = {row[0] for row in db.execute("""
            SELECT a.listing_id FROM match_alerts a
            JOIN account_profiles p ON p.search_id = a.search_id
            WHERE p.id = ? AND a.scan_id = ?
        """, (account_id, scan_id))}
        items = [_item_summary(item) for item in scan.get("listings") or []
                 if str(item.get("id")) in matched_ids]
        result = {"scan_id": scan_id,
                  "keyword": ", ".join(scan.get("keywords") or [])[:100],
                  "items": items}
        db.execute("""
            INSERT OR IGNORE INTO email_batch_scans
                (account_id, batch_key, scan_id, result_json, completed_at)
            VALUES (?, ?, ?, ?, ?)
        """, (account_id, batch_key, scan_id, json.dumps(result), _now()))
        rows = db.execute("""
            SELECT result_json FROM email_batch_scans
            WHERE account_id = ? AND batch_key = ? ORDER BY completed_at, scan_id
        """, (account_id, batch_key)).fetchall()
        if len(rows) < batch["expected_scans"]:
            return False
        results = [json.loads(row[0]) for row in rows]
        if not any(row["items"] for row in results):
            db.execute("UPDATE email_batches SET closed_at = ? "
                       "WHERE account_id = ? AND batch_key = ?",
                       (_now(), account_id, batch_key))
            return False
        base_url = _public_base_url()
        if not base_url:
            # Reconcile this batch after a public host is configured.
            return False
        subject, body = _summary_text(results, base_url)
        db.execute("""
            INSERT OR IGNORE INTO email_outbox
                (account_id, batch_key, recipient, subject, body_text,
                 created_at, next_attempt_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (account_id, batch_key, batch["recipient"], subject, body,
              _now(), _now()))
        db.execute("UPDATE email_batches SET closed_at = ? "
                   "WHERE account_id = ? AND batch_key = ?",
                   (_now(), account_id, batch_key))
        return True


def smtp_configured() -> bool:
    return all(os.environ.get(name) for name in (
        "SCOUT_SMTP_HOST", "SCOUT_SMTP_USER", "SCOUT_SMTP_PASSWORD",
        "SCOUT_SMTP_FROM"))


def _send_smtp(recipient: str, subject: str, body_text: str) -> None:
    host = os.environ["SCOUT_SMTP_HOST"]
    port = int(os.environ.get("SCOUT_SMTP_PORT", "587"))
    username = os.environ["SCOUT_SMTP_USER"]
    password = os.environ["SCOUT_SMTP_PASSWORD"]
    sender = normalize_email(os.environ["SCOUT_SMTP_FROM"])
    message = EmailMessage()
    message["From"] = sender
    message["To"] = normalize_email(recipient)
    message["Subject"] = subject
    message.set_content(body_text)
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)


def check_smtp_connection() -> None:
    """Authenticate to the configured sender without sending a message."""
    if not smtp_configured():
        raise RuntimeError("Set SCOUT_SMTP_HOST, USER, PASSWORD, and FROM first")
    host = os.environ["SCOUT_SMTP_HOST"]
    port = int(os.environ.get("SCOUT_SMTP_PORT", "587"))
    username = os.environ["SCOUT_SMTP_USER"]
    password = os.environ["SCOUT_SMTP_PASSWORD"]
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as smtp:
            smtp.login(username, password)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(username, password)


def deliver_pending_summaries(db_path: Path = ALERT_DB,
                              sender: Callable[[str, str, str], None] | None = None) -> int:
    """Retry queued messages; never send when SMTP is not configured."""
    if sender is None and not smtp_configured():
        return 0
    if not _send_lock.acquire(blocking=False):
        return 0
    sent = 0
    try:
        with closing(_connect(db_path)) as db:
            rows = db.execute("""
                SELECT o.id, o.recipient, o.subject, o.body_text, o.attempt_count
                FROM email_outbox o
                JOIN account_profiles p ON p.id = o.account_id
                WHERE o.sent_at IS NULL AND o.next_attempt_at <= ?
                  AND p.email_notifications_enabled = 1
                ORDER BY o.created_at, o.id LIMIT 20
            """, (_now(),)).fetchall()
        for row in rows:
            try:
                (sender or _send_smtp)(row["recipient"], row["subject"],
                                       row["body_text"])
            except Exception as error:
                delay = min(3600, 60 * 2 ** min(row["attempt_count"], 6))
                retry_at = (datetime.now(timezone.utc) +
                            timedelta(seconds=delay)).isoformat()
                with closing(_connect(db_path)) as db, db:
                    db.execute("""
                        UPDATE email_outbox
                        SET attempt_count = attempt_count + 1,
                            next_attempt_at = ?, last_error = ?
                        WHERE id = ? AND sent_at IS NULL
                    """, (retry_at, f"{type(error).__name__}: {error}"[:300], row["id"]))
            else:
                with closing(_connect(db_path)) as db, db:
                    db.execute("UPDATE email_outbox SET sent_at = ?, last_error = NULL "
                               "WHERE id = ? AND sent_at IS NULL", (_now(), row["id"]))
                sent += 1
    finally:
        _send_lock.release()
    return sent


def email_status(db_path: Path = ALERT_DB) -> dict:
    with closing(_connect(db_path)) as db:
        pending, sent = db.execute("""
            SELECT SUM(sent_at IS NULL), SUM(sent_at IS NOT NULL)
            FROM email_outbox
        """).fetchone()
    return {"configured": smtp_configured(), "pending": pending or 0,
            "sent": sent or 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Inspect or send Scout email summaries")
    parser.add_argument("action", choices=["status", "send", "check"],
                        nargs="?", default="status")
    args = parser.parse_args()
    if args.action == "send":
        print(f"Sent {deliver_pending_summaries()} summary email(s).")
    elif args.action == "check":
        check_smtp_connection()
        print("SMTP login succeeded; no email was sent.")
    print(json.dumps(email_status(), indent=2))
