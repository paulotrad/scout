"""Conservative text clues for walk-behind mower price cohorts."""

from __future__ import annotations

import re


WALK_BEHIND_TYPES = {"lawn_mower", "push_mower", "self_propelled_mower"}
KNOWN_COHORTS = {
    "battery_push_mower", "gas_push_mower", "corded_push_mower",
    "battery_self_propelled_mower", "gas_self_propelled_mower",
    "corded_self_propelled_mower", "battery_lawn_mower",
    "gas_lawn_mower", "corded_lawn_mower",
}
BRANDS = ("ryobi", "craftsman", "toro", "troy-bilt", "greenworks",
          "ego", "echo", "kobalt", "honda", "murray", "bolens",
          "yard machines", "cub cadet", "snapper", "husqvarna")
GAS_CUE = re.compile(
    r"\b(?:power\s+source\s+gas|gas[ -]?powered|gas\s+(?:push\s+)?(?:lawn\s+)?mower|"
    r"petrol|gasoline|\d{2,3}\s*cc|briggs(?:\s*(?:&|and)\s*)?stratton|"
    r"(?:honda|kohler)\s+(?:gcv\d+\s+)?engine|gcv\d+|engine|"
    r"carburetor|carb\b|pull[ -]?start|(?:first|second)\s+(?:pull|yank)|"
    r"fresh\s+oil|oil\s+changed|spark\s+plug)\b",
    re.I)
BATTERY_CUE = re.compile(
    r"\b(?:power\s+source\s+battery|battery[ -]?powered|cordless|"
    r"lithium[ -]?ion|one\+\s*\d{2}\s*v|\d{2}\s*v\s+(?:battery|mower)|"
    r"(?:battery|mower)\s*\d{2}\s*v|v20\s+brushless)\b",
    re.I)
DECK_SIZE = re.compile(r'\b(\d{2})\s*(?:-?\s*(?:inches|inch|in)\b\.?|")', re.I)
VOLTAGE = re.compile(r"\b(\d{2})\s*v\b|\bv(20)\b", re.I)


def mower_variant(title: str, description: str = "",
                  reference_type: str | None = None) -> dict | None:
    """Use explicit seller clues; unknown details never become assumed matches."""
    if reference_type not in WALK_BEHIND_TYPES:
        return None
    title = str(title or "")
    description = str(description or "")
    text = f"{title} {description}"
    gas_context = re.sub(r"\b(?:no|without)\s+gas\b", "", text, flags=re.I)
    gas_context = re.sub(
        r"\bgas\s+(?:lawn\s+)?mower\s+(?:isn.?t|is\s+not|not)\s+"
        r"(?:needed|required|necessary)\b", "", gas_context, flags=re.I)
    gas = bool(GAS_CUE.search(gas_context))
    battery = bool(BATTERY_CUE.search(text))
    if gas and battery:
        # A gas mower can have a starter battery. Only a direct cordless or
        # battery-powered claim makes this a genuine seller contradiction.
        explicit_battery = bool(re.search(
            r"\b(?:cordless|battery[ -]?powered|power\s+source\s+battery)\b",
            text, re.I))
        power = "conflict" if explicit_battery else "gas"
    elif gas:
        power = "gas"
    elif battery:
        power = "battery"
    elif re.search(r"\bcorded\b", text, re.I):
        power = "corded"
    elif re.search(r"\belectric\b", text, re.I):
        power = "electric_unknown"
    else:
        power = "unknown"
    brand = next((name for name in BRANDS if re.search(
        rf"\b{re.escape(name)}\b", title, re.I)), None)
    deck = DECK_SIZE.search(text)
    volts = VOLTAGE.search(text) if power == "battery" else None
    kit = "not_applicable"
    if power == "battery":
        if re.search(r"\b(?:no|without|missing)\s+(?:the\s+|a\s+)?batter(?:y|ies)\b|"
                     r"\btool[ -]?only\b|\bbatter(?:y|ies)\s+not\s+included\b",
                     text, re.I):
            kit = "tool_only"
        elif re.search(r"\b(?:no|without|missing)\s+(?:the\s+|a\s+)?charger\b|"
                       r"\bcharger\s+(?:not\s+included|sold\s+separately)\b",
                       text, re.I):
            kit = "no_charger"
        elif (re.search(r"\b(?:includes?|comes?\s+with|with)\b.{0,85}\bbatter(?:y|ies)\b",
                         text, re.I) and re.search(r"\bcharger\b", text, re.I)) or (
             re.search(r"\bbatter(?:y|ies)\b.{0,35}\bcharger\b.{0,20}\bincluded\b",
                       text, re.I)):
            kit = "battery_and_charger"
        else:
            kit = "unknown"
    return {
        "power_source": power,
        "brand": brand,
        "deck_inches": int(deck.group(1)) if deck else None,
        "voltage": int(next(value for value in volts.groups() if value)) if volts else None,
        "kit": kit,
    }


def mower_peer(target: dict, other: dict) -> bool:
    """Only compare walk-behind mowers with the same known power setup."""
    if target["power_source"] != other["power_source"]:
        return False
    power = target["power_source"]
    if power not in {"battery", "gas", "corded"}:
        return False
    width = target["deck_inches"]
    other_width = other["deck_inches"]
    if width is not None and (other_width is None or abs(width - other_width) > 2):
        return False
    if power == "battery":
        return (target["brand"] is not None and target["brand"] == other["brand"] and
                target["voltage"] is not None and
                target["voltage"] == other["voltage"] and
                target["kit"] != "unknown" and target["kit"] == other["kit"])
    if target["brand"] and target["brand"] != other["brand"]:
        return False
    return True


def mower_category(reference_type: str, variant: dict) -> dict:
    """A known power/form cohort or a specific category proposal for review."""
    power = variant["power_source"]
    key = f"{power}_{reference_type}"
    return {
        "parent": "garden_equipment/lawn_mower",
        "key": key,
        "label": key.replace("_", " ").title(),
        "status": "known" if key in KNOWN_COHORTS else "needs_evidence",
        "attributes": variant,
    }
