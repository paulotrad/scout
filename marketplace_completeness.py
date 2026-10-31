"""Conservative seller-text checks for incomplete physical products."""

from __future__ import annotations

import re

from product_candidate import suggest_product_candidate


COMPONENTS = {
    "riding_mower": {"cutting deck": r"(?:cutting\s+)?decks?", "engine": r"engines?",
                     "wheels": r"wheels?|tires?", "transmission": r"transmissions?"},
    "zero_turn_mower": {"cutting deck": r"(?:cutting\s+)?decks?", "engine": r"engines?",
                        "wheels": r"wheels?|tires?", "transmission": r"transmissions?"},
    "electric_scooter": {"battery": r"batter(?:y|ies)(?!\s+charger)", "motor": r"motors?",
                         "wheels": r"wheels?|tires?"},
    "mobility_scooter": {"battery": r"batter(?:y|ies)(?!\s+charger)", "motor": r"motors?",
                         "wheels": r"wheels?|tires?"},
    "dresser": {"drawer": r"drawers?"},
    "cabinet": {"door": r"doors?", "drawer": r"drawers?"},
    "china_cabinet": {"door": r"doors?", "drawer": r"drawers?"},
    "file_cabinet": {"drawer": r"drawers?"},
    "kitchen_cabinet": {"door": r"doors?", "drawer": r"drawers?"},
    "dining_table": {"tabletop": r"(?:table\s*)?tops?", "legs": r"legs?"},
    "coffee_table": {"tabletop": r"(?:table\s*)?tops?", "legs": r"legs?"},
    "end_table": {"tabletop": r"(?:table\s*)?tops?", "legs": r"legs?"},
    "table": {"tabletop": r"(?:table\s*)?tops?", "legs": r"legs?"},
    "refrigerator": {"compressor": r"compressors?", "door": r"doors?"},
    "freezer": {"compressor": r"compressors?", "door": r"doors?"},
    "washer": {"drum": r"drums?", "motor": r"motors?", "door": r"doors?"},
    "dryer": {"drum": r"drums?", "motor": r"motors?", "door": r"doors?"},
    "dishwasher": {"motor": r"motors?", "door": r"doors?"},
    "stove": {"burner": r"burners?", "door": r"doors?"},
}
WALK_BEHIND = {"lawn_mower", "push_mower", "self_propelled_mower",
               "commercial_walk_behind_mower", "robot_mower"}
WALK_BEHIND_COMPONENTS = {"cutting deck": r"(?:cutting\s+)?decks?",
                          "engine or motor": r"engines?|motors?",
                          "wheels": r"wheels?|tires?", "blade": r"blades?"}
PARTS_ONLY_TITLE = re.compile(
    r"\b(?:for\s+parts|parts\s+only|parts\s+mower|scrap\s+only|"
    r"(?:frame|chassis)\s+only)\b", re.I)
PARTS_ONLY_DESCRIPTION = re.compile(
    r"(?:^|[.!?]\s*)\s*(?:for\s+parts|parts\s+only|scrap\s+only|"
    r"selling\s+(?:it\s+)?for\s+parts|sold\s+for\s+parts|"
    r"just\s+(?:the\s+)?(?:frame|chassis))\b", re.I)


def _missing(text: str, component: str) -> bool:
    """Require a direct missing-part claim; 'no deck problems' is not one."""
    pattern = (
        rf"\b(?:no|without|missing|lacks?|lost)\s+"
        rf"(?:(?:a|an|the|one|two|all|any|its|original|front|rear|left|right|new)\s+){{0,3}}"
        rf"(?:{component})\b(?!\s+(?:issues?|problems?|damage|rust|wear|concerns?|"
        rf"scratches?|dents?|cracks?))"
        rf"|\b(?:{component})\s+(?:is|are|was|were)\s+"
        rf"(?:missing|removed|gone|not\s+included)\b"
    )
    return any(not text[max(0, match.start() - 3):match.start()].casefold().endswith("no ")
               for match in re.finditer(pattern, text, re.I))


def incomplete_product(listing: dict) -> dict | None:
    """Return clear missing-part evidence without inferring from photos or price."""
    title = str(listing.get("title") or "")
    description = str(listing.get("description") or "")
    text = f"{title} {description}"
    candidate = suggest_product_candidate(title)
    types = candidate.get("types") or []
    kind = types[0]["type"] if len(types) == 1 and not candidate.get("needs_review") else None
    components = (WALK_BEHIND_COMPONENTS if kind in WALK_BEHIND or (
                  kind is None and re.search(r"\bmower\b", title, re.I)) else
                  COMPONENTS.get(kind, {}))
    missing = [name for name, pattern in components.items() if _missing(text, pattern)]
    if missing:
        return {"reason": "missing_major_components", "missing_components": missing}
    if PARTS_ONLY_TITLE.search(title) or PARTS_ONLY_DESCRIPTION.search(description):
        return {"reason": "parts_only_listing", "missing_components": []}
    return None
