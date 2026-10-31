"""Identify seller-offered furniture bundles without pricing the whole set as one item."""

from __future__ import annotations

import re


# Longest, specific names come before generic names so "dining chairs" are not
# counted a second time as generic chairs.
ITEMS = (
    ("sectional", r"sectional(?:\s+(?:sofa|couch))?s?"),
    ("sofa_bed", r"(?:sleeper\s+sofas?|sofa\s+beds?|pull[ -]?out\s+couches?|futons?)"),
    ("loveseat", r"(?:love\s*seats?|loveseats?)"),
    ("sofa", r"(?:sofas?|couch(?:es)?)"),
    ("office_chair", r"(?:office|desk|gaming)\s+chairs?"),
    ("dining_chair", r"(?:dining(?:\s+room)?\s+chairs?|sillas?)"),
    ("rocking_chair", r"rocking\s+chairs?"),
    ("recliner_chair", r"(?:recliner|reclining)\s+chairs?|recliners?"),
    ("accent_chair", r"(?:(?:accent|arm)\s*|wing[ -]?back\s+)chairs?"),
    ("ottoman", r"(?:ottomans?|foot\s*stools?)"),
    ("chair", r"chairs?"),
    ("dining_table", r"(?:dining(?:\s+room)?\s+tables?|mesas?)"),
    ("coffee_table", r"coffee\s+tables?"),
    ("end_table", r"(?:end|side)\s+tables?"),
    ("table", r"tables?"),
    ("dresser", r"dressers?"),
    ("nightstand", r"night\s*stands?"),
    ("china_cabinet", r"china\s+cabinets?"),
    ("file_cabinet", r"(?:file|filing)\s+cabinets?"),
    ("cabinet", r"cabinets?"),
    ("washer_dryer_combo", r"washer[ -]?dryer\s+combos?"),
    ("washer", r"(?:washing\s+machines?|washers?)"),
    ("dryer", r"(?:clothes?\s+dryers?|dryers?)"),
    ("refrigerator", r"(?:refrigerators?|fridges?)"),
    ("dishwasher", r"dishwashers?"),
    ("stove", r"(?:stoves?|cooktops?|ranges?)"),
    ("freezer", r"freezers?"),
    ("microwave", r"microwaves?"),
)
NOUNS = re.compile("|".join(rf"(?P<{kind}>\b(?:{pattern})\b)"
                            for kind, pattern in ITEMS), re.I)
QUANTITY = re.compile(
    r"\b(?P<count>\d{1,2}|one|two|three|four|five|six|seven|eight|a|an|"
    r"pair\s+of)\s+(?:(?:matching|wooden|leather|fabric|metal|small|large|old|used)\s+){0,2}$",
    re.I)
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4,
                "five": 5, "six": 6, "seven": 7, "eight": 8,
                "a": 1, "an": 1, "pair of": 2}
OFFER_CUE = re.compile(
    r"\b(?:together|bundle|set\s+of|includes?|comes?\s+with|selling|"
    r"both|all\s+for|pair\s+of)\b", re.I)
ASK_UNCLEAR = re.compile(r"\b(?:each|per\s+(?:chair|piece|item)|separately|"
                         r"separate\s+or\s+together|individual(?:ly)?)\b", re.I)
EXCLUSION = re.compile(r"\b(?:not\s+included|not\s+for\s+sale|"
                       r"only\s+the\s+\w+\s+is\s+included)\b", re.I)
MATERIAL = re.compile(
    r"\b(solid\s+wood|faux\s+leather|leather|wooden|velvet|microfiber|"
    r"metal|glass)\s+$", re.I)


def _parts(text: str) -> list[dict]:
    found = []
    for match in NOUNS.finditer(text):
        kind = match.lastgroup
        noun = match.group(0)
        prefix = text[max(0, match.start() - 32):match.start()]
        quantity = QUANTITY.search(prefix)
        material = MATERIAL.search(prefix)
        if quantity:
            raw = quantity.group("count").casefold()
            count = NUMBER_WORDS.get(raw, int(raw) if raw.isdigit() else None)
        else:
            count = None if noun.casefold().rstrip().endswith("s") else 1
        trailing = re.match(r"\s*(?:x|×|\()\s*(\d{1,2})\b",
                            text[match.end():match.end() + 10], re.I)
        if trailing:
            count = int(trailing.group(1))
        found.append({"product_type": kind, "quantity": count,
                      "material": material.group(1).lower() if material else None,
                      "mention": noun.strip(), "start": match.start(),
                      "end": match.end()})
    return found


def _is_set(parts: list[dict], text: str) -> bool:
    if any((part["quantity"] or 0) > 1 or part["quantity"] is None
           for part in parts):
        return True
    return any(len(between) <= 40 and "." not in between and
               re.search(r"\b(?:and|with|plus|y)\b|[,&+/]", between, re.I)
               for first, second in zip(parts, parts[1:])
               if (between := text[first["end"]:second["start"]]))


def extract_bundle(listing: dict) -> dict | None:
    """Return component quantities only when the seller describes a set."""
    title = str(listing.get("title") or "")
    description = str(listing.get("description") or "")
    title_parts = _parts(title)
    description_parts = _parts(description) if OFFER_CUE.search(description) else []
    title_is_set = _is_set(title_parts, title)
    description_is_set = _is_set(description_parts, description)
    if not title_is_set and not description_is_set:
        return None
    parts = title_parts if title_is_set else description_parts
    # The description often clarifies a terse title such as "Furniture set".
    if title_is_set and description_is_set and all(
            part["quantity"] is not None for part in description_parts):
        parts = description_parts
    totals = {}
    for part in parts:
        kind = part["product_type"]
        material = part["material"]
        key = (kind, material)
        quantity = part["quantity"]
        if key in totals:
            previous = totals[key]
            previous["quantity"] = (previous["quantity"] + quantity
                                    if previous["quantity"] is not None and
                                    quantity is not None else None)
        else:
            label = kind.replace("_", " ").title()
            totals[key] = {"product_type": kind, "quantity": quantity,
                           "material": material,
                           "label": f"{material.title()} {label}" if material else label}
    components = list(totals.values())
    uncertain = (EXCLUSION.search(description) is not None or
                 any(part["quantity"] is None for part in components) or
                 sum(part["quantity"] or 0 for part in components) > 25)
    return {"status": "needs_review" if uncertain else "confirmed",
            "components": components,
            "ask_scope_unclear": bool(ASK_UNCLEAR.search(title + " " + description)),
            "reason": ("Bundle inclusions or quantities need review" if uncertain else "")}
