"""Small, explainable product-type suggestions from listing titles.

These labels help route an item to a catalog search. They are not a claim that
the exact manufacturer, model, material, or condition has been confirmed.
"""

from __future__ import annotations

import re


PRODUCT_TYPES = (
    ("china_cabinet", "furniture", r"\bchina\s+cabinet\b"),
    ("file_cabinet", "furniture", r"\b(?:filing|file)\s+cabinet\b"),
    ("kitchen_cabinet", "furniture", r"\b(?:kitchen|bathroom)\s+cabinets?\b"),
    ("cabinet", "furniture", r"\bcabinets?\b"),
    ("dresser", "furniture", r"\bdresser\b"),
    ("nightstand", "furniture", r"\bnight\s*stands?\b"),
    ("ottoman", "furniture", r"\b(?:ottoman|foot\s*stool)\b"),
    ("desk", "furniture", r"\bdesk\b"),
    ("dining_table", "furniture", r"\bdining\s+(?:room\s+)?table\b"),
    ("coffee_table", "furniture", r"\bcoffee\s+table\b"),
    ("end_table", "furniture", r"\b(?:end|side)\s+table\b"),
    ("office_chair", "furniture", r"\b(?:office|desk|gaming)\s+chair\b"),
    ("rocking_chair", "furniture", r"\brocking\s+chair\b"),
    ("recliner_chair", "furniture", r"\b(?:recliner\s+chair|reclining\s+chair|recliner)\b"),
    ("dining_chair", "furniture", r"\bdining\s+(?:room\s+)?chairs?\b"),
    ("accent_chair", "furniture", r"\b(?:accent|arm)\s*chairs?\b"),
    ("chair", "furniture", r"\bchairs?\b"),
    ("piano", "musical_instrument", r"\bpiano\b"),
    ("sectional", "furniture", r"\bsectional\b"),
    ("loveseat", "furniture", r"\b(?:love\s*seat|loveseat)\b"),
    ("sofa_bed", "furniture",
     r"\b(?:sleeper\s+sofa|sofa\s+bed|pull[\s-]*out\s+(?:sofa|couch)|futon)\b"),
    ("sofa", "furniture", r"\b(?:sofa|couch)\b"),
    ("table", "furniture", r"\btable\b"),
    ("refrigerator", "appliance", r"\b(?:refrigerator|fridge)\b"),
    ("washer_dryer_combo", "appliance", r"\bwasher[ -]?dryer\s+combo\b"),
    ("washer", "appliance", r"\b(?:washing\s+machine|washer)\b"),
    ("dryer", "appliance", r"\b(?:clothes?\s+dryer|dryer)\b"),
    ("dishwasher", "appliance", r"\bdishwasher\b"),
    ("stove", "appliance", r"\b(?:stove|cooktop|range)\b"),
    ("freezer", "appliance", r"\bfreezer\b"),
    ("microwave", "appliance", r"\bmicrowave\b"),
    ("zero_turn_mower", "garden_equipment",
     r"\bzero[ -]?turn\s+(?:riding\s+)?(?:lawn\s*)?mower\b"),
    ("riding_mower", "garden_equipment", r"\b(?:riding|ride[ -]?on)\s+(?:lawn\s*)?mower\b|\blawn\s+tractor\b"),
    ("commercial_walk_behind_mower", "garden_equipment",
     r"\bcommercial\s+(?:walk[ -]?behind\s+)?(?:lawn\s*)?mower\b"),
    ("self_propelled_mower", "garden_equipment",
     r"\bself[ -]?propelled\b.{0,45}\b(?:lawn\s*)?mower\b|"
     r"\b(?:lawn\s*)?mower\b.{0,45}\bself[ -]?propelled\b"),
    ("reel_mower", "garden_equipment", r"\b(?:manual\s+)?reel\s+(?:lawn\s*)?mower\b"),
    ("push_mower", "garden_equipment", r"\bpush\s+(?:lawn\s*)?mower\b"),
    ("robot_mower", "garden_equipment", r"\b(?:robot(?:ic)?|automower)\s+(?:lawn\s*)?mower\b"),
    ("lawn_mower", "garden_equipment", r"\b(?:lawn\s*mower|lawnmower)\b"),
    ("mobility_scooter", "mobility_equipment", r"\bmobility\s+scooter\b"),
    ("electric_scooter", "personal_transport", r"\b(?:electric|e[ -]?)\s*(?:kick\s+)?scooter\b"),
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
    title = re.sub(r"[+_/]", " ", title)
    title = re.sub(r"\bself\s*-\s*propelled\b", "self propelled", title,
                   flags=re.I)
    title = re.sub(r"\bzero\s*-\s*turn\b", "zero turn", title,
                   flags=re.I)
    title = re.sub(r"\blaw\s+mower\b", "lawn mower", title, flags=re.I)
    matches = [
        {"type": item_type, "category": category, "evidence": match.group(0)}
        for item_type, category, pattern in PRODUCT_TYPES
        if (match := re.search(pattern, title, flags=re.I))
    ]
    if any(row["type"] in {"sectional", "loveseat", "sofa_bed"} for row in matches):
        # "Sectional sofa" and "loveseat couch" name one piece, not two.
        if not re.search(r"\b(?:and|plus)\b|&", title, flags=re.I):
            matches = [row for row in matches if row["type"] != "sofa"]
    specific_to_generic = {
        "cabinet": {"china_cabinet", "file_cabinet", "kitchen_cabinet"},
        "table": {"dining_table", "coffee_table", "end_table"},
        "chair": {"office_chair", "rocking_chair", "recliner_chair",
                  "dining_chair", "accent_chair"},
        "riding_mower": {"zero_turn_mower"},
        "lawn_mower": {"zero_turn_mower", "riding_mower", "commercial_walk_behind_mower",
                       "self_propelled_mower",
                       "reel_mower", "push_mower", "robot_mower"},
        "washer": {"washer_dryer_combo"},
        "dryer": {"washer_dryer_combo"},
    }
    present = {row["type"] for row in matches}
    matches = [row for row in matches if not (
        row["type"] in specific_to_generic and
        present.intersection(specific_to_generic[row["type"]]))]
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
