"""Small, explainable product-type suggestions from listing titles.

These labels help route an item to a catalog search. They are not a claim that
the exact manufacturer, model, material, or condition has been confirmed.
"""

from __future__ import annotations

import re


PRODUCT_TYPES = (
    ("china_cabinet", "furniture", r"\bchina\s+cabinet\b"),
    ("dresser", "furniture", r"\bdresser\b"),
    ("desk", "furniture", r"\bdesk\b"),
    ("piano", "musical_instrument", r"\bpiano\b"),
    ("sectional", "furniture", r"\bsectional\b"),
    ("loveseat", "furniture", r"\b(?:love\s*seat|loveseat)\b"),
    ("sofa_bed", "furniture",
     r"\b(?:sleeper\s+sofa|sofa\s+bed|pull[\s-]*out\s+(?:sofa|couch)|futon)\b"),
    ("sofa", "furniture", r"\b(?:sofa|couch)\b"),
    ("table", "furniture", r"\btable\b"),
    ("lawn_mower", "garden_equipment", r"\b(?:lawn\s*mower|lawnmower)\b"),
    ("aquarium", "pet_equipment", r"\b(?:aquarium|fish\s+tank)\b"),
)

MATERIAL_CLUES = (
    ("solid_wood", r"\bsolid\s+wood\b"),
    ("wood", r"\bwood(?:en)?\b"),
    ("metal", r"\bmetal\b"),
    ("glass", r"\bglass\b"),
    ("leather", r"\bleather\b"),
    ("faux_leather", r"\b(?:faux|fake|vegan)\s+leather\b"),
    ("velvet", r"\bvelvet\b"),
    ("microfiber", r"\bmicrofiber\b"),
)


def suggest_product_candidate(title: str) -> dict:
    """Suggest a broad type and explicit material clues for catalog retrieval."""
    matches = [
        {"type": item_type, "category": category, "evidence": match.group(0)}
        for item_type, category, pattern in PRODUCT_TYPES
        if (match := re.search(pattern, title, flags=re.I))
    ]
    if any(row["type"] in {"sectional", "loveseat", "sofa_bed"} for row in matches):
        # "Sectional sofa" and "loveseat couch" name one piece, not two.
        if not re.search(r"\b(?:and|plus)\b|&", title, flags=re.I):
            matches = [row for row in matches if row["type"] != "sofa"]
    materials = [name for name, pattern in MATERIAL_CLUES
                 if re.search(pattern, title, flags=re.I)]
    if "faux_leather" in materials and "leather" in materials:
        materials.remove("leather")
    if "solid_wood" in materials and "wood" in materials:
        materials.remove("wood")
    return {
        "types": matches,
        "material_clues": materials,
        "type_confidence": "medium" if len(matches) == 1 else "low",
        "needs_review": len(matches) != 1,
        "next_step": ("search_product_catalogs" if len(matches) == 1 else
                      "split_or_identify_items" if matches else "inspect_listing_details"),
    }
