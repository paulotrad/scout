"""Durable, match-only email digests for completed Scout scan batches."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
import json
import math
import os
import re
import sqlite3
import smtplib
import ssl
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlsplit

from marketplace_alerts import ALERT_DB, _connect, normalize_email
from marketplace_db import get_image


_send_lock = threading.Lock()
_BATCH_KEY = re.compile(r"[a-z0-9:-]{1,128}\Z")
_IMAGE_ID = re.compile(r"[0-9]{5,25}\Z")
MAX_INLINE_IMAGES = 60
MAX_INLINE_BYTES = 6 * 1024 * 1024


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


def _amount(value: object) -> int | float | None:
    return (value if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0 else None)


def _facebook_url(item: dict) -> str:
    """Send only canonical Marketplace item links from a Facebook URL."""
    raw = str(item.get("url") or "")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    found = re.fullmatch(r"/marketplace/item/(\d{5,25})/?", parsed.path)
    if (parsed.scheme != "https" or parsed.hostname not in {
            "facebook.com", "www.facebook.com", "m.facebook.com"} or
            parsed.username or parsed.password or not found or
            found.group(1) != str(item.get("id") or "")):
        return ""
    return f"https://www.facebook.com/marketplace/item/{found.group(1)}/"


def alert_item_summary(item: dict, keyword: str = "") -> dict:
    price = _amount(item.get("asking_price"))
    distance = item.get("distance_miles_approx")
    valuation = item.get("valuation") or {}
    rating = valuation.get("price_category") or {}
    description = (item.get("generated_description") or
                   item.get("refactored_description") or
                   item.get("description") or "")
    summary = {
        "id": str(item.get("id") or "")[:80],
        "title": " ".join(str(item.get("display_title") or item.get("title") or
                              "Marketplace item").split())[:180],
        "price": price,
        "price_source": str(item.get("price_source") or "")[:40],
        "card_price": _amount(item.get("card_asking_price")),
        "condition": ("" if item.get("pet_listing") else
                      str(item.get("condition_grade") or "unknown")[:40]),
        "pet_listing": bool(item.get("pet_listing")),
        "location": " ".join(str(item.get("location") or "").split())[:100],
        "distance_miles": round(distance, 1) if isinstance(distance, (int, float))
        and not isinstance(distance, bool) and math.isfinite(distance) else None,
        "description": " ".join(str(description).split())[:240],
        "item_type": ("assorted items" if item.get("mixed_lot") else
                      str(item.get("item_type") or "").replace("_", " "))[:60],
        "mixed_lot": bool(item.get("mixed_lot")),
        "price_rating": str(rating.get("label") or "")[:40],
        "price_rating_code": str(rating.get("code") or "")[:30],
        "comparison_count": rating.get("comparison_count") if isinstance(
            rating.get("comparison_count"), int) else valuation.get("comparable_count"),
        "comparison_low": _amount(valuation.get("low")),
        "comparison_high": _amount(valuation.get("high")),
        "reference_scope_miles": valuation.get("reference_scope_miles"),
        "reference_scope_label": str(valuation.get("reference_scope_label") or "")[:30],
        "valuation_reason": str(valuation.get("reason") or "")[:80],
        "comparables_needed": (valuation.get("needed") if isinstance(
            valuation.get("needed"), int) else 3),
        "itemized_bundle": valuation.get("price_type") == "itemized_asking_range",
        "bundle_breakdown": [{"label": str(part.get("label") or "Item")[:50],
                              "quantity": int(part.get("quantity") or 0),
                              "unit_low": _amount(part.get("unit_low")),
                              "unit_high": _amount(part.get("unit_high"))}
                             for part in (valuation.get("bundle_components") or [])[:10]
                             if isinstance(part, dict)],
        "facebook_url": _facebook_url(item),
    }
    term = keyword.strip().casefold()
    if term == "free":
        summary["match_source"] = "free_card" if price == 0 else "search"
    elif term and term in str(item.get("title") or "").casefold():
        summary["match_source"] = "seller_title"
    elif term and term in str(item.get("description") or "").casefold():
        summary["match_source"] = "seller_description"
    elif term and term in str(item.get("item_type") or "").replace("_", " ").casefold():
        summary["match_source"] = "ai_item_type"
    else:
        summary["match_source"] = "search"
    summary["reason"] = _alert_reason(summary, keyword)
    summary["comparison_label"] = _comparison_label(summary)
    summary["price_note"] = _price_note(summary)
    return summary


def _brief_price(value: int | float) -> str:
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def _comparison_label(item: dict) -> str:
    if item.get("pet_listing"):
        return "Price comparison not offered for pets"
    if item.get("mixed_lot"):
        return "Mixed items; one price comparison would be misleading"
    reason = item.get("valuation_reason") or ""
    if reason == "missing_major_components":
        return "Complete-item prices do not apply; a major part is missing"
    if reason == "parts_only_listing":
        return "Parts-only listing; complete-item prices do not apply"
    if reason == "mower_power_source_unclear":
        return "Price comparison pending: mower power source needs confirmation"
    if reason == "scooter_variant_details_missing":
        return "Price comparison pending: scooter model needs confirmation"
    if reason in {"price_group_unavailable", "item_identity_unconfirmed"}:
        return "No reliable comparison group for this item yet"
    if reason in {"insufficient_local_furniture_comparables",
                  "insufficient_local_product_comparables",
                  "insufficient_same_variant_comparables",
                  "insufficient_bundle_component_comparables",
                  "insufficient_verified_comparables"}:
        found = item.get("comparison_count")
        needed = item.get("comparables_needed") or 3
        if isinstance(found, int) and isinstance(needed, int) and found < needed:
            return (f"Price comparison pending: {found} of {needed} "
                    "compatible asking prices found")
    low, high = item.get("comparison_low"), item.get("comparison_high")
    count = item.get("comparison_count")
    if low is None or high is None or high < low or not isinstance(count, int) or count < 3:
        return "Local price comparison unavailable"
    scope = item.get("reference_scope_label") or "local"
    miles = item.get("reference_scope_miles")
    distance = (f" within about {miles:g} miles"
                if isinstance(miles, (int, float)) and 0 < miles <= 500 else "")
    reference = (f"{scope.title()} asking reference {_brief_price(low)}–"
                 f"{_brief_price(high)} from {count} similar listings{distance}")
    if item.get("itemized_bundle"):
        reference = (f"Combined individual asking reference {_brief_price(low)}–"
                     f"{_brief_price(high)} from at least {count} single-item prices "
                     "per component; no bundle sale price is assumed")
    return f"{item['price_rating']} · {reference}" if item.get("price_rating") else reference


def _alert_reason(item: dict, keyword: str) -> str:
    search = f"your {keyword.strip()[:60]} search" if keyword.strip() else "your saved search"
    distance = (f" about {item['distance_miles']:g} mi away"
                if item.get("distance_miles") is not None else "")
    source = item.get("match_source")
    if source == "free_card":
        match = f"Facebook listed it as free in {search}"
    elif source == "seller_title":
        match = f"Seller title matches {search}"
    elif source == "seller_description":
        match = f"Seller description matches {search}"
    elif source == "ai_item_type":
        match = f"Scout identified an item matching {search}"
    else:
        match = f"Found in {search}"
    match += distance
    rating = item.get("price_rating")
    if item.get("itemized_bundle") and rating:
        return (f"{match}; {rating.lower()} against summed "
                "individual asking prices, not whole-bundle listings.")
    if rating and item.get("comparison_low") is not None and item.get("comparison_high") is not None:
        scope = item.get("reference_scope_label") or "local"
        return f"{match}; {rating.lower()} among similar {scope} asking prices."
    return match + "."


def _price_note(item: dict) -> str:
    if item.get("price_source") in {"seller_description", "seller_title"} and item.get("card_price") == 0:
        source = ("description" if item["price_source"] == "seller_description"
                  else "title")
        return (f"Facebook card says FREE; seller {source} asks " +
                _price_label(item.get("price")) + ".")
    if item.get("price_source") == "unconfirmed":
        return "Seller price needs confirmation."
    return ""


def _public_base_url() -> str:
    host = os.environ.get("SCOUT_PUBLIC_HOST", "").strip().lower()
    if not re.fullmatch(r"[a-z0-9.-]+", host):
        return ""
    return f"https://{host}/notify"


def _price_label(price: object) -> str:
    return ("FREE" if price == 0 else f"${price:,.2f}"
            if isinstance(price, (int, float)) else "Price unavailable")


def _result_url(base_url: str, row: dict, item: dict) -> str:
    return (f"{base_url}/scan/{quote(row['scan_id'])}"
            f"#item-{quote(item['id'])}")


def _feedback_url(base_url: str, row: dict, item: dict, vote: str) -> str:
    return (f"{base_url}/scan/{quote(row['scan_id'])}"
            f"?feedback={vote}&listing={quote(item['id'])}"
            f"#item-{quote(item['id'])}")


def _summary_text(rows: list[dict], base_url: str) -> tuple[str, str]:
    matches = [(row, item) for row in rows for item in row["items"]]
    if len(matches) == 1:
        item = matches[0][1]
        subject = f"Scout: {item['title']} — {_price_label(item['price'])}"[:140]
    else:
        subject = (f"Scout: {len(matches)} new finds — " +
                   ", ".join(item["title"] for _, item in matches[:2]))[:140]
    lines = [f"{len(matches)} new Marketplace match{'es' if len(matches) != 1 else ''}",
             "Seller prices, nearby asking references, and links:", ""]
    for number, (row, item) in enumerate(matches, 1):
        lines.append(f"{number}. {item['title']} — {_price_label(item['price'])}")
        place = item["location"] or "Location unavailable"
        if item.get("distance_miles") is not None:
            place += f" · about {item['distance_miles']:g} mi away"
        lines.append(f"   {place} · Search: {row['keyword']}")
        if not item.get("pet_listing"):
            condition = item["condition"].replace("_", " ")
            lines.append("   Condition: " +
                         ("varies by item" if item.get("mixed_lot") else
                          "unconfirmed" if condition == "unknown" else
                          f"{condition} (AI estimate)"))
        if reason := (item.get("reason") or _alert_reason(item, row["keyword"])):
            lines.append(f"   Why this matched: {reason}")
        lines.append(f"   {_comparison_label(item)}")
        for part in item.get("bundle_breakdown") or []:
            if part["unit_low"] is not None and part["unit_high"] is not None:
                lines.append(f"   {part['quantity']} × {part['label']}: "
                             f"{_brief_price(part['unit_low'])}–"
                             f"{_brief_price(part['unit_high'])} each")
        if note := _price_note(item):
            lines.append(f"   Check price: {note}")
        if item.get("description"):
            lines.append(f"   Details: {item['description']}")
        if item.get("facebook_url"):
            lines.append(f"   Open Facebook listing: {item['facebook_url']}")
        lines.append(f"   View Scout's photos and analysis: {_result_url(base_url, row, item)}")
        lines.append("   Is Scout's analysis right?")
        lines.append(f"   Yes: {_feedback_url(base_url, row, item, 'up')}")
        lines.append(f"   Needs correction: {_feedback_url(base_url, row, item, 'down')}")
        lines.append("")
    lines.extend(["Prices are seller asking prices. Confirm availability and listing "
                  "details with the seller.",
                  "You received this because your Scout watchlist found new matches."])
    return subject, "\n".join(lines) + "\n"


def _summary_html(rows: list[dict], base_url: str) -> str:
    matches = [(row, item) for row in rows for item in row["items"]]
    cards = []
    for row, item in matches:
        url = escape(_result_url(base_url, row, item), quote=True)
        title = escape(item["title"])
        price = escape(_price_label(item["price"]))
        place = escape(item["location"] or "Location unavailable")
        distance = (f" · about {item['distance_miles']:g} mi away"
                    if item.get("distance_miles") is not None else "")
        details = (f'<p style="margin:8px 0;color:#526477;line-height:1.5">'
                   f'{escape(item["description"])}</p>' if item.get("description") else "")
        condition = item["condition"].replace("_", " ")
        condition_label = ("" if item.get("pet_listing") else
                           "Condition varies by item" if item.get("mixed_lot") else
                           "Condition unconfirmed" if condition == "unknown" else
                           f"{condition.title()} condition (AI estimate)")
        comparison = escape(_comparison_label(item))
        bundle_lines = "".join(
            f'<li>{part["quantity"]} × {escape(part["label"])}: '
            f'{escape(_brief_price(part["unit_low"]))}–'
            f'{escape(_brief_price(part["unit_high"]))} each</li>'
            for part in item.get("bundle_breakdown") or []
            if part["unit_low"] is not None and part["unit_high"] is not None)
        bundle_html = (f'<ul style="margin:6px 0;padding-left:20px;color:#526477;'
                       f'font-size:13px">{bundle_lines}</ul>' if bundle_lines else "")
        reason = escape(item.get("reason") or _alert_reason(item, row["keyword"]))
        reason_html = (f'<p style="margin:5px 0;color:#526477;font-size:13px;line-height:1.5">'
                       f'Why it matched: {reason}</p>' if reason else "")
        note = _price_note(item)
        price_note = (f'<p style="margin:8px 0;color:#a84725;font-size:13px">'
                      f'{escape(note)}</p>' if note else "")
        photo = (f'<img src="cid:scout-{item["image_id"]}@scout.local" '
                 f'width="128" height="128" alt="Photo of {title}" '
                 'style="display:block;width:128px;height:128px;object-fit:cover;'
                 'border-radius:10px">' if item.get("image_id") else "")
        photo_cell = (f'<td width="140" valign="top" style="padding-right:12px">'
                      f'{photo}</td>' if photo else "")
        yes_url = escape(_feedback_url(base_url, row, item, "up"), quote=True)
        no_url = escape(_feedback_url(base_url, row, item, "down"), quote=True)
        feedback_html = (
            '<p style="margin:14px 0 5px;color:#526477;font-size:13px">'
            'Is Scout\'s analysis right?</p>'
            f'<a href="{yes_url}" style="display:inline-block;padding:8px 12px;'
            'margin-right:8px;border:1px solid #bfd9d3;border-radius:8px;'
            'color:#0d5e54;text-decoration:none;font-weight:700">👍 Yes</a>'
            f'<a href="{no_url}" style="display:inline-block;padding:8px 12px;'
            'border:1px solid #d5dce1;border-radius:8px;color:#36485a;'
            'text-decoration:none;font-weight:700">👎 Needs correction</a>')
        cards.append(
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="background:#ffffff;border:1px solid #dce5e9;border-radius:14px;'
            'margin:0 0 14px 0"><tr><td style="padding:16px">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'{photo_cell}<td valign="top">'
            f'<p style="margin:0 0 5px;color:#087969;font-size:18px;font-weight:800">Seller asks {price}</p>'
            f'<h2 style="margin:0 0 6px;color:#18283d;font-size:17px;line-height:1.3">{title}</h2>'
            f'<p style="margin:0;color:#526477;font-size:13px">{place}{distance}</p>'
            '</td></tr></table>'
            f'<p style="margin:12px 0 5px;color:#17283d;font-size:14px;font-weight:700">'
            f'{comparison}</p>'
            f'{bundle_html}'
            f'{reason_html}{price_note}{details}'
            f'<p style="margin:9px 0;color:#526477;font-size:13px">'
            f'{escape((item.get("item_type") or row["keyword"]).title())} · '
            f'{escape(condition_label) + " · " if condition_label else ""}'
            f'Search: {escape(row["keyword"])}</p>'
            + (f'<a href="{escape(item["facebook_url"], quote=True)}" '
               'style="display:inline-block;background:#087969;color:#ffffff;'
               'text-decoration:none;padding:10px 14px;border-radius:8px;'
               'font-weight:700;margin-right:8px;margin-top:8px">Open on Facebook</a>'
               if item.get("facebook_url") else "") +
            f'<a href="{url}" style="display:inline-block;background:#e9f1ef;color:#0d5e54;'
            'text-decoration:none;padding:10px 14px;border-radius:8px;'
            'font-weight:700;margin-top:8px">View Scout analysis</a>'
            f'{feedback_html}</td></tr></table>')
    return ('<!doctype html><html><body style="margin:0;padding:0;background:#f3f7f6;'
            'font-family:Arial,Helvetica,sans-serif">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            '<tr><td align="center" style="padding:20px 12px">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="max-width:620px"><tr><td>'
            '<p style="margin:0 0 4px;color:#087969;font-size:13px;font-weight:800;'
            'letter-spacing:2px">SCOUT</p>'
            f'<h1 style="margin:0 0 8px;color:#18283d;font-size:25px">'
            f'{len(matches)} new match{"es" if len(matches) != 1 else ""}</h1>'
            '<p style="margin:0 0 20px;color:#526477;line-height:1.5">'
            'Fresh items from your saved Marketplace searches.</p>'
            + ''.join(cards) +
            '<p style="margin:18px 0;color:#526477;font-size:12px;line-height:1.5">'
            'Prices are seller asking prices. Confirm availability and listing details '
            'with the seller. Scout emails each new listing once per person.</p>'
            '</td></tr></table></td></tr></table></body></html>')


def _available_image_ids(db: sqlite3.Connection, rows: list[dict]) -> list[str]:
    candidates = list(dict.fromkeys(
        str(item.get("id") or "") for row in rows for item in row["items"]
        if _IMAGE_ID.fullmatch(str(item.get("id") or ""))))[:MAX_INLINE_IMAGES]
    if not candidates:
        return []
    try:
        found = {row[0]: (row[1], row[2]) for row in db.execute(
            "SELECT listing_id, mime_type, length(image_data) FROM images "
            "WHERE source = ? AND listing_id IN (" +
            ",".join("?" for _ in candidates) + ")",
            ("facebook_marketplace", *candidates))}
    except sqlite3.OperationalError:
        return []
    selected, total_bytes = [], 0
    for item_id in candidates:
        mime, size = found.get(item_id, (None, None))
        if (mime not in {"image/jpeg", "image/png", "image/webp"} or
                not isinstance(size, int) or size > 512 * 1024 or
                total_bytes + size > MAX_INLINE_BYTES):
            continue
        selected.append(item_id)
        total_bytes += size
    return selected


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
        keyword = ", ".join(scan.get("keywords") or [])[:100]
        items = [alert_item_summary(item, keyword) for item in scan.get("listings") or []
                 if str(item.get("id")) in matched_ids]
        result = {"scan_id": scan_id,
                  "keyword": keyword,
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
        image_ids = _available_image_ids(db, results)
        for result in results:
            for item in result["items"]:
                if str(item.get("id")) in image_ids:
                    item["image_id"] = str(item["id"])
        subject, body = _summary_text(results, base_url)
        body_html = _summary_html(results, base_url)
        db.execute("""
            INSERT OR IGNORE INTO email_outbox
                (account_id, batch_key, recipient, subject, body_text,
                 body_html, image_ids_json, created_at, next_attempt_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (account_id, batch_key, batch["recipient"], subject, body,
              body_html, json.dumps(image_ids), _now(), _now()))
        db.execute("UPDATE email_batches SET closed_at = ? "
                   "WHERE account_id = ? AND batch_key = ?",
                   (_now(), account_id, batch_key))
        return True


def smtp_configured() -> bool:
    return all(os.environ.get(name) for name in (
        "SCOUT_SMTP_HOST", "SCOUT_SMTP_USER", "SCOUT_SMTP_PASSWORD",
        "SCOUT_SMTP_FROM"))


def _build_message(sender: str, recipient: str, subject: str, body_text: str,
                   body_html: str | None = None,
                   image_ids: list[str] | None = None,
                   db_path: Path = ALERT_DB) -> EmailMessage:
    message = EmailMessage()
    message["From"] = normalize_email(sender)
    message["To"] = normalize_email(recipient)
    message["Subject"] = subject
    message.set_content(body_text)
    if body_html:
        message.add_alternative(body_html, subtype="html")
        html_part = message.get_body(preferencelist=("html",))
        total_bytes = 0
        for item_id in (image_ids or [])[:MAX_INLINE_IMAGES]:
            if not _IMAGE_ID.fullmatch(item_id):
                continue
            try:
                found = get_image(db_path, "facebook_marketplace", item_id)
            except sqlite3.Error:
                found = None
            if not found:
                continue
            mime_type, data = found
            if (mime_type not in {"image/jpeg", "image/png", "image/webp"} or
                    len(data) > 512 * 1024 or total_bytes + len(data) > MAX_INLINE_BYTES):
                continue
            html_part.add_related(data, maintype="image",
                                  subtype=mime_type.split("/", 1)[1],
                                  cid=f"<scout-{item_id}@scout.local>",
                                  disposition="inline",
                                  filename=f"scout-{item_id}.{mime_type.split('/', 1)[1]}")
            total_bytes += len(data)
    return message


def _send_smtp(recipient: str, subject: str, body_text: str,
               body_html: str | None = None,
               image_ids: list[str] | None = None,
               db_path: Path = ALERT_DB) -> None:
    host = os.environ["SCOUT_SMTP_HOST"]
    port = int(os.environ.get("SCOUT_SMTP_PORT", "587"))
    username = os.environ["SCOUT_SMTP_USER"]
    password = os.environ["SCOUT_SMTP_PASSWORD"]
    sender = normalize_email(os.environ["SCOUT_SMTP_FROM"])
    message = _build_message(sender, recipient, subject, body_text,
                             body_html, image_ids, db_path)
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
                SELECT o.id, o.recipient, o.subject, o.body_text,
                       o.body_html, o.image_ids_json, o.attempt_count
                FROM email_outbox o
                JOIN account_profiles p ON p.id = o.account_id
                WHERE o.sent_at IS NULL AND o.next_attempt_at <= ?
                  AND p.email_notifications_enabled = 1
                ORDER BY o.created_at, o.id LIMIT 20
            """, (_now(),)).fetchall()
        for row in rows:
            try:
                if sender:
                    sender(row["recipient"], row["subject"], row["body_text"])
                else:
                    _send_smtp(row["recipient"], row["subject"], row["body_text"],
                               row["body_html"],
                               json.loads(row["image_ids_json"] or "[]"), db_path)
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
