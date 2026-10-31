"""Local SQLite storage for Marketplace listings, price observations, and images."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any


DEFAULT_DB = Path("runs/marketplace.sqlite")
MAX_IMAGE_BYTES = 8 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    refactored_description TEXT,
    source_url TEXT,
    seller_location TEXT,
    search_zip TEXT,
    asking_price REAL,
    currency TEXT,
    identity_json TEXT,
    condition_json TEXT,
    valuation_json TEXT,
    vision_json TEXT,
    condition_assessment_json TEXT,
    second_opinion_json TEXT,
    couch_quality_json TEXT,
    description_summary_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_listings_search
    ON listings (search_zip, source, title);

CREATE TABLE IF NOT EXISTS price_history (
    source TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    asking_price REAL NOT NULL,
    currency TEXT,
    PRIMARY KEY (source, listing_id, observed_at),
    FOREIGN KEY (source, listing_id) REFERENCES listings(source, listing_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS images (
    source TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    image_data BLOB NOT NULL,
    byte_count INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (source, listing_id),
    FOREIGN KEY (source, listing_id) REFERENCES listings(source, listing_id)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS listing_images (
    source TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    mime_type TEXT NOT NULL,
    image_data BLOB NOT NULL,
    byte_count INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (source, listing_id, position),
    FOREIGN KEY (source, listing_id) REFERENCES listings(source, listing_id)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS scan_seen (
    scope_key TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    card_signature TEXT,
    PRIMARY KEY (scope_key, listing_id)
);
CREATE TABLE IF NOT EXISTS scan_scope_seeded (
    scope_key TEXT PRIMARY KEY,
    seeded_at TEXT NOT NULL
);
"""


def _image_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Image has an unsupported or invalid signature")


def _image_from_run(base_dir: Path, relative_name: str) -> tuple[str, bytes]:
    relative = Path(relative_name)
    if relative.is_absolute():
        raise ValueError("Image path must be relative to the run JSON")
    root = base_dir.resolve()
    image_path = (root / relative).resolve()
    if not image_path.is_relative_to(root):
        raise ValueError("Image path leaves the run directory")
    data = image_path.read_bytes()
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image is empty or exceeds the 8 MiB limit")
    return _image_type(data), data


def _price(value: Any) -> float | None:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) and 0 <= amount < 10_000_000 else None


def _json_field(record: dict, key: str) -> str | None:
    value = record.get(key)
    if (key in {"vision", "condition_assessment", "second_opinion", "couch_quality"} and
            isinstance(value, dict) and
            value.get("status") == "skipped_price_range"):
        return None
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _ensure_listing_columns(db: sqlite3.Connection) -> None:
    """Add new evidence fields to databases created by earlier project versions."""
    present = {row[1] for row in db.execute("PRAGMA table_info(listings)")}
    for column in ("description", "refactored_description", "vision_json",
                   "condition_assessment_json", "second_opinion_json", "couch_quality_json",
                   "description_summary_json"):
        if column not in present:
            db.execute(f"ALTER TABLE listings ADD COLUMN {column} TEXT")


def _upsert_listing(db: sqlite3.Connection, row: dict, source: str,
                    captured_at: str) -> bool:
    listing_id = str(row.get("listing_id") or "").strip()
    title = str(row.get("title") or "").strip()
    if not listing_id or not title:
        return False
    asking_price = _price(row.get("asking_price"))
    db.execute("""
        INSERT INTO listings (
            source, listing_id, title, description, refactored_description,
            source_url, seller_location,
            search_zip, asking_price, currency, identity_json, condition_json,
            valuation_json, vision_json, condition_assessment_json, second_opinion_json,
            couch_quality_json,
            description_summary_json,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, listing_id) DO UPDATE SET
            title = excluded.title,
            description = COALESCE(excluded.description, listings.description),
            refactored_description = COALESCE(excluded.refactored_description,
                                              listings.refactored_description),
            source_url = COALESCE(excluded.source_url, listings.source_url),
            seller_location = COALESCE(excluded.seller_location, listings.seller_location),
            search_zip = COALESCE(excluded.search_zip, listings.search_zip),
            asking_price = COALESCE(excluded.asking_price, listings.asking_price),
            currency = COALESCE(excluded.currency, listings.currency),
            identity_json = COALESCE(excluded.identity_json, listings.identity_json),
            condition_json = COALESCE(excluded.condition_json, listings.condition_json),
            valuation_json = COALESCE(excluded.valuation_json, listings.valuation_json),
            vision_json = COALESCE(excluded.vision_json, listings.vision_json),
            condition_assessment_json = COALESCE(excluded.condition_assessment_json,
                                                 listings.condition_assessment_json),
            second_opinion_json = COALESCE(excluded.second_opinion_json,
                                           listings.second_opinion_json),
            couch_quality_json = COALESCE(excluded.couch_quality_json,
                                          listings.couch_quality_json),
            description_summary_json = COALESCE(excluded.description_summary_json,
                                                listings.description_summary_json),
            last_seen_at = excluded.last_seen_at
    """, (source, listing_id, title, row.get("description") or None,
          row.get("refactored_description") or None,
          row.get("url") or row.get("source_url"), row.get("location"),
          row.get("search_zip"), asking_price,
          row.get("currency") or "USD", _json_field(row, "identity"),
          _json_field(row, "condition"), _json_field(row, "valuation"),
          _json_field(row, "vision"), _json_field(row, "condition_assessment"),
          _json_field(row, "second_opinion"),
          _json_field(row, "couch_quality"),
          _json_field(row, "description_summary"),
          captured_at, captured_at))
    if asking_price is not None:
        db.execute("""
            INSERT OR IGNORE INTO price_history
                (source, listing_id, observed_at, asking_price, currency)
            VALUES (?, ?, ?, ?, ?)
        """, (source, listing_id, captured_at, asking_price,
              row.get("currency") or "USD"))
    return True


def store_result(result: dict, run_dir: Path, db_path: Path = DEFAULT_DB) -> dict:
    """Save one run atomically; safely import its already downloaded thumbnails."""
    captured_at = result.get("captured_at") or datetime.now(timezone.utc).isoformat()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    saved_images = 0
    saved_listings = 0
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        db.executescript(SCHEMA)
        _ensure_listing_columns(db)
        with db:
            for comp in result.get("comparables", []):
                source = comp.get("source")
                if source in {"facebook_marketplace", "craigslist"}:
                    saved_listings += _upsert_listing(db, comp, source, captured_at)
            for listing in result.get("listings", []):
                if not _upsert_listing(db, listing, "facebook_marketplace", captured_at):
                    continue
                saved_listings += 1
                image_file = listing.get("image_file")
                if not image_file:
                    continue
                try:
                    mime_type, data = _image_from_run(run_dir, image_file)
                except FileNotFoundError:
                    # A previous import may hold the thumbnail after a local
                    # file was removed. Keep that BLOB while updating labels.
                    existing = db.execute("""
                        SELECT 1 FROM images WHERE source = ? AND listing_id = ?
                    """, ("facebook_marketplace", str(listing["listing_id"]))).fetchone()
                    if existing:
                        continue
                    raise
                db.execute("""
                    INSERT INTO images
                        (source, listing_id, mime_type, image_data, byte_count,
                         sha256, captured_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, listing_id) DO UPDATE SET
                        mime_type = excluded.mime_type,
                        image_data = excluded.image_data,
                        byte_count = excluded.byte_count,
                        sha256 = excluded.sha256,
                        captured_at = excluded.captured_at
                    WHERE images.sha256 <> excluded.sha256
                """, ("facebook_marketplace", str(listing["listing_id"]),
                      mime_type, sqlite3.Binary(data), len(data),
                      hashlib.sha256(data).hexdigest(), captured_at))
                saved_images += 1
                gallery = listing.get("image_files")
                if not isinstance(gallery, list) or not gallery:
                    gallery = [image_file]
                for position, relative in enumerate(gallery[:4]):
                    if not isinstance(relative, str):
                        continue
                    try:
                        gallery_mime, gallery_data = _image_from_run(run_dir, relative)
                    except FileNotFoundError:
                        # Keep the BLOB imported by a prior pipeline stage.
                        if db.execute("""
                            SELECT 1 FROM listing_images
                            WHERE source = ? AND listing_id = ? AND position = ?
                        """, ("facebook_marketplace", str(listing["listing_id"]),
                              position)).fetchone():
                            continue
                        raise
                    db.execute("""
                        INSERT INTO listing_images (
                            source, listing_id, position, mime_type, image_data,
                            byte_count, sha256, captured_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source, listing_id, position) DO UPDATE SET
                            mime_type = excluded.mime_type,
                            image_data = excluded.image_data,
                            byte_count = excluded.byte_count,
                            sha256 = excluded.sha256,
                            captured_at = excluded.captured_at
                        WHERE listing_images.sha256 <> excluded.sha256
                    """, ("facebook_marketplace", str(listing["listing_id"]),
                          position, gallery_mime, sqlite3.Binary(gallery_data),
                          len(gallery_data), hashlib.sha256(gallery_data).hexdigest(),
                          captured_at))
    return {"listings_processed": saved_listings, "images_stored": saved_images,
            "database_file": str(db_path.resolve())}


def import_run(run_path: Path, db_path: Path = DEFAULT_DB) -> dict:
    data = json.loads(run_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("listings"), list):
        raise ValueError("Run JSON must be an object with a listings array")
    return store_result(data, run_path.parent, db_path)


def get_image(db_path: Path, source: str, listing_id: str) -> tuple[str, bytes] | None:
    """Read an image back from SQLite, including its validated MIME type."""
    with closing(sqlite3.connect(db_path)) as db:
        row = db.execute("""
            SELECT mime_type, image_data FROM images
            WHERE source = ? AND listing_id = ?
        """, (source, listing_id)).fetchone()
    return (row[0], row[1]) if row else None


def get_images(db_path: Path, source: str, listing_id: str,
               limit: int = 4) -> list[tuple[str, bytes]]:
    """Return ordered gallery BLOBs, with the legacy primary as a fallback."""
    with closing(sqlite3.connect(db_path)) as db:
        try:
            rows = db.execute("""
                SELECT mime_type, image_data FROM listing_images
                WHERE source = ? AND listing_id = ? ORDER BY position LIMIT ?
            """, (source, listing_id, max(1, min(limit, 4)))).fetchall()
        except sqlite3.OperationalError:
            rows = []
    if rows:
        return [(mime, data) for mime, data in rows]
    primary = get_image(db_path, source, listing_id)
    return [primary] if primary else []


def get_cached_analysis(db_path: Path, source: str, listing_id: str) -> dict:
    """Read prior model labels without loading the thumbnail BLOB."""
    if not db_path.is_file():
        return {}
    with closing(sqlite3.connect(db_path)) as db:
        try:
            row = db.execute("""
                SELECT vision_json, condition_assessment_json, second_opinion_json,
                       couch_quality_json, description_summary_json,
                       refactored_description
                FROM listings
                WHERE source = ? AND listing_id = ?
            """, (source, listing_id)).fetchone()
        except sqlite3.OperationalError:
            return {}
    if row is None:
        return {}
    cached = {}
    for key, value in zip(("vision", "condition_assessment", "second_opinion",
                           "couch_quality", "description_summary"), row[:5]):
        if value:
            try:
                cached[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    cached["refactored_description"] = row[5]
    return cached


def known_listing_ids(db_path: Path, source: str,
                      listing_ids: list[str]) -> set[str]:
    """Find previously collected cards without reading images or model data."""
    ids = list(dict.fromkeys(str(value) for value in listing_ids if value))
    if not ids or not db_path.is_file():
        return set()
    known = set()
    with closing(sqlite3.connect(db_path)) as db:
        for offset in range(0, len(ids), 500):
            group = ids[offset:offset + 500]
            try:
                rows = db.execute(
                    "SELECT listing_id FROM listings WHERE source = ? "
                    "AND listing_id IN (" + ",".join("?" for _ in group) + ")",
                    (source, *group))
                known.update(str(row[0]) for row in rows)
            except sqlite3.OperationalError:
                return set()
    return known


def known_scan_ids(db_path: Path, scope_key: str,
                   listing_ids: list[str]) -> set[str]:
    """Return cards already completed for this account and exact watch search."""
    ids = list(dict.fromkeys(str(value) for value in listing_ids if value))
    if not ids or not db_path.is_file():
        return set()
    known = set()
    with closing(sqlite3.connect(db_path)) as db:
        for offset in range(0, len(ids), 500):
            group = ids[offset:offset + 500]
            try:
                rows = db.execute(
                    "SELECT listing_id FROM scan_seen WHERE scope_key = ? "
                    "AND listing_id IN (" + ",".join("?" for _ in group) + ")",
                    (scope_key, *group))
                known.update(str(row[0]) for row in rows)
            except sqlite3.OperationalError:
                return set()
    return known


def card_signature(card: dict) -> str:
    """Stable fingerprint of the title and price visible before detail collection."""
    title = " ".join(str(card.get("title") or "").casefold().split())
    price = _price(card.get("asking_price"))
    payload = json.dumps([title, f"{price:.2f}" if price is not None else None],
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ensure_scan_seen_signature(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS scan_seen ("
               "scope_key TEXT NOT NULL, listing_id TEXT NOT NULL, "
               "seen_at TEXT NOT NULL, card_signature TEXT, "
               "PRIMARY KEY (scope_key, listing_id))")
    if "card_signature" not in {row[1] for row in db.execute("PRAGMA table_info(scan_seen)")}:
        db.execute("ALTER TABLE scan_seen ADD COLUMN card_signature TEXT")


def unchanged_scan_cards(db_path: Path, scope_key: str,
                         cards: list[dict]) -> set[str]:
    """Skip only cards whose visible title and price match the last review."""
    if not cards or not db_path.is_file():
        return set()
    signatures = {str(card["listing_id"]): card_signature(card) for card in cards
                  if card.get("listing_id")}
    unchanged = set()
    with closing(sqlite3.connect(db_path)) as db, db:
        _ensure_scan_seen_signature(db)
        ids = list(signatures)
        for offset in range(0, len(ids), 500):
            group = ids[offset:offset + 500]
            rows = db.execute(
                "SELECT listing_id, card_signature FROM scan_seen WHERE scope_key = ? "
                "AND listing_id IN (" + ",".join("?" for _ in group) + ")",
                (scope_key, *group))
            unchanged.update(str(listing_id) for listing_id, signature in rows
                             if signature == signatures.get(str(listing_id)))
    return unchanged


def mark_scan_seen(db_path: Path, scope_key: str,
                   listing_ids: list[str], *, seeded: bool = False,
                   cards_by_id: dict[str, dict] | None = None) -> None:
    """Mark collected cards after their search finishes successfully."""
    ids = list(dict.fromkeys(str(value) for value in listing_ids if value))
    if not ids and not seeded:
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as db, db:
        _ensure_scan_seen_signature(db)
        now = datetime.now(timezone.utc).isoformat()
        cards = cards_by_id or {}
        db.executemany("""INSERT INTO scan_seen
                       (scope_key, listing_id, seen_at, card_signature)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(scope_key, listing_id) DO UPDATE SET
                         seen_at = excluded.seen_at,
                         card_signature = COALESCE(excluded.card_signature,
                                                   scan_seen.card_signature)""",
                       ((scope_key, listing_id, now,
                         card_signature(cards[listing_id]) if listing_id in cards else None)
                        for listing_id in ids))
        if seeded:
            db.execute("CREATE TABLE IF NOT EXISTS scan_scope_seeded ("
                       "scope_key TEXT PRIMARY KEY, seeded_at TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO scan_scope_seeded VALUES (?, ?)",
                       (scope_key, now))


def scope_is_seeded(db_path: Path, scope_key: str) -> bool:
    if not db_path.is_file():
        return False
    with closing(sqlite3.connect(db_path)) as db:
        try:
            return db.execute("SELECT 1 FROM scan_scope_seeded WHERE scope_key = ?",
                              (scope_key,)).fetchone() is not None
        except sqlite3.OperationalError:
            return False


def database_stats(db_path: Path) -> dict:
    with closing(sqlite3.connect(db_path)) as db:
        return {
            "listings": db.execute("SELECT COUNT(*) FROM listings").fetchone()[0],
            "price_observations": db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0],
            "images": db.execute("SELECT COUNT(*) FROM images").fetchone()[0],
            "image_bytes": db.execute("SELECT COALESCE(SUM(byte_count), 0) FROM images").fetchone()[0],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and import Marketplace SQLite data")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    commands = parser.add_subparsers(dest="command", required=True)
    import_cmd = commands.add_parser("import-run", help="Import an existing run JSON and its thumbnails")
    import_cmd.add_argument("run_json", type=Path)
    commands.add_parser("stats", help="Count stored listings, observations, and images")
    export_cmd = commands.add_parser("export-image", help="Read one image back from SQLite")
    export_cmd.add_argument("source", choices=["facebook_marketplace", "craigslist"])
    export_cmd.add_argument("listing_id")
    export_cmd.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "import-run":
        print(json.dumps(import_run(args.run_json, args.db), indent=2))
    elif args.command == "stats":
        print(json.dumps(database_stats(args.db), indent=2))
    else:
        found = get_image(args.db, args.source, args.listing_id)
        if not found:
            parser.error("No stored image for that source and listing ID")
        mime_type, data = found
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(data)
        print(f"Saved {mime_type} ({len(data)} bytes) to {args.output.resolve()}")


if __name__ == "__main__":
    main()
