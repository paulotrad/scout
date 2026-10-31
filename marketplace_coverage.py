"""Coverage counts for reviewed mower price categories."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from marketplace_mower import KNOWN_COHORTS, WALK_BEHIND_TYPES, mower_category, mower_variant


def mower_coverage(rows: list[dict], as_of: datetime | None = None) -> dict:
    """Count distinct, recent references; a cohort count is not a price estimate."""
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Coverage reference time must include a timezone")
    counts: Counter[str] = Counter()
    needs_review = 0
    seen = set()
    for row in rows:
        kind = row.get("reference_type")
        if kind not in WALK_BEHIND_TYPES or not row.get("price_reviewed"):
            continue
        observed = row.get("observed_at")
        if not isinstance(observed, str):
            continue
        try:
            observed_at = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        except ValueError:
            continue
        if observed_at.tzinfo is None or not timedelta(0) <= now - observed_at <= timedelta(days=365):
            continue
        identity = (row.get("source"), str(row.get("listing_id") or ""))
        if not identity[1] or identity in seen:
            continue
        seen.add(identity)
        variant = mower_variant(row.get("title", ""), row.get("description", ""), kind)
        if variant is None:
            continue
        cohort = mower_category(kind, variant)
        if cohort["status"] == "known":
            counts[cohort["key"]] += 1
        else:
            needs_review += 1
    return {
        "categories": [{"key": key, "label": key.replace("_", " ").title(),
                        "count": counts[key]}
                       for key in sorted(KNOWN_COHORTS)],
        "needs_category_review": needs_review,
        "total_walk_behind": len(seen),
        "window_days": 365,
    }
