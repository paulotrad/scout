"""Recover product titles when Marketplace displays an old price on its own line."""

from __future__ import annotations

import re


PRICE_ONLY = re.compile(r"^\$\s*([\d,]+(?:\.\d{1,2})?)$")
LOCATION = re.compile(r"^.{2,70},\s*[A-Z]{2}$")


def repair_price_only_title(listing: dict) -> bool:
    """Use the next card line as the title while preserving the old price."""
    title = str(listing.get("title") or "").strip()
    match = PRICE_ONLY.fullmatch(title)
    lines = listing.get("raw_card_text")
    if not match or not isinstance(lines, list):
        return False
    try:
        title_index = lines.index(title)
    except ValueError:
        return False
    for line in lines[title_index + 1:]:
        if not isinstance(line, str):
            continue
        candidate = line.strip()
        if not candidate or candidate.casefold() == "free" or PRICE_ONLY.fullmatch(candidate):
            continue
        if LOCATION.fullmatch(candidate):
            break
        listing["original_card_title"] = title
        listing["previous_price"] = float(match.group(1).replace(",", ""))
        listing["title"] = candidate
        listing["title_source"] = "card_line_after_previous_price"
        return True
    return False
