"""Create card copy from saved 8B vision evidence without adding new claims."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import unicodedata

from marketplace_atomic import write_json

COPY_VERSION = "grounded_photo_description_v5"
FREE_PREFIX = re.compile(r"^free+\b[\s:!.,;\-–—]*", re.I)
FREE_WORD = re.compile(r"\bfree+\b", re.I)
PROMO_PREFIX = re.compile(r"^(?:beautiful|gorgeous|amazing|lovely)\b[\s:!.,;\-–—]*", re.I)
TITLE_ACRONYMS = re.compile(r"\b(?:tv|led|oled|pc|usb|gpu|cpu|bmw|rtx|hd|ho)\b", re.I)
REPAIR_CUE = re.compile(
    r"\b(?:broken|not working|stopped working|no longer|does not|doesn't|"
    r"needs? repair|needs? a new|missing|damaged)\b", re.I)
VAGUE_LOT_TITLE = re.compile(
    r"^(?:free\s+)?(?:stuff|misc(?:ellaneous)?(?:\s+(?:items?|furniture))?|"
    r"assorted\s+items?|various\s+items?)\b", re.I)
UNIDENTIFIED_LOT_TITLE = re.compile(
    r"^(?:free\s+)?(?:stuff|curbside\s+items?|misc(?:ellaneous)?(?:\s+items?)?|"
    r"assorted\s+items?|various\s+items?)\s*[!.,;:]*$", re.I)
ADDRESS_CUE = re.compile(
    r"\b(?:\d{2,6}\s+(?:block\s+)?|curb(?:side)?\b|"
    r"(?:road|rd|street|st|avenue|ave|drive|dr)\b)", re.I)
LOT_NOUNS = re.compile(
    r"\b(?:fan|shoe\s+rack|rack|scooter|(?:dog|cat)\s+bed|harness|bibs?|"
    r"play\s*(?:yard|pen)|pack\s*n\s*play|toys?|chairs?|tables?|"
    r"couches?|sofas?|dressers?|cabinets?|lamps?|microwaves?|"
    r"boxes?|books?|clothes?|shoes?|bikes?|strollers?)\b", re.I)


def mixed_item_lot(title: str, description: str) -> bool:
    """Flag a vague listing that names several different offered objects."""
    if not VAGUE_LOT_TITLE.match(str(title or "").strip()):
        return False
    names = {re.sub(r"\s+", " ", match.group(0).casefold())
             for match in LOT_NOUNS.finditer(str(description or ""))}
    return len(names) >= 3


def unidentified_lot(title: str, description: str) -> bool:
    """A vague curbside post has no named item for Scout to verify."""
    seller_title = str(title or "").strip()
    vague = (UNIDENTIFIED_LOT_TITLE.fullmatch(seller_title) or
             re.match(r"^(?:free\s+)?(?:@\s*|at\s+)?(?:the\s+)?curb\b",
                      seller_title, re.I))
    return bool(vague and not LOT_NOUNS.search(
        seller_title + " " + str(description or "")))


def display_title(title: str, asking_price: object, item_type: str = "",
                  description: str = "") -> str:
    """Show an item name while keeping the seller's title in the details."""
    def trim_edges(value: str) -> str:
        edge_punctuation = "!.,;:-–—*•~�\"'“”‘’"
        while value and (value[0].isspace() or value[0] in edge_punctuation or
                         unicodedata.category(value[0]) in {"So", "Sk", "Mn", "Pi", "Pf"}):
            value = value[1:]
        while value and (value[-1].isspace() or value[-1] in edge_punctuation or
                         unicodedata.category(value[-1]) in {"So", "Sk", "Mn", "Pi", "Pf"}):
            value = value[:-1]
        return value

    source = " ".join(str(title or "").split())
    # Long seller headlines sometimes lead with a slogan and repeat the price.
    # Keep the identified material and its actual container instead.
    if (re.search(r"\b(?:waste|used)\s+(?:(?:automotive|motor|engine)\s+)?oil\b",
                  source, re.I) and
            re.search(r"\b(?:automotive|motor|engine|oil\s+changes?)\b",
                      source, re.I)):
        container = (" in jugs" if re.search(r"\bjugs\b", source, re.I) else
                     " in a jug" if re.search(r"\bjug\b", source, re.I) else "")
        return "Used automotive oil" + container
    if mixed_item_lot(source, description):
        names = list(dict.fromkeys(
            re.sub(r"\s+", " ", match.group(0).casefold())
            for match in LOT_NOUNS.finditer(description)))
        if len(names) >= 3:
            return ", ".join(names[:3]).capitalize() + " and more"
    cleaned = trim_edges(source)
    cleaned = re.sub(r"^\$\s*\d+(?:[.,]\d{1,2})?(?=\s|$)", "", cleaned)
    cleaned = re.sub(r"\s+\$\s*\d+(?:[.,]\d{1,2})?(?:\s+obo)?\s*$",
                     "", cleaned, flags=re.I)
    # The separate price label already says FREE. Protect product names where
    # "free" describes the item rather than its asking price.
    protected = []
    def protect(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"ScoutProtectedWord{len(protected) - 1}"
    cleaned = re.sub(r"\b(?:free people|free[- ]standing|free weights)\b",
                     protect, cleaned, flags=re.I)
    had_free = bool(FREE_WORD.search(cleaned))
    cleaned = FREE_WORD.sub(" ", cleaned)
    for index, value in enumerate(protected):
        cleaned = cleaned.replace(f"ScoutProtectedWord{index}", value)
    if had_free:
        cleaned = re.sub(r"\b(?:for|to go)\s*$", "", cleaned, flags=re.I)
        cleaned = re.sub(r"^pick\s+up\b", "", cleaned, flags=re.I)
        cleaned = trim_edges(" ".join(cleaned.split()))
        without_promo = PROMO_PREFIX.sub("", cleaned, count=1)
        if without_promo and without_promo != cleaned:
            cleaned = trim_edges(without_promo)
    cleaned = re.sub(r"\b(?:porch\s+pick\s*up\s+only|pickup\s+only)\b.*$",
                     "", cleaned, flags=re.I)
    cleaned = trim_edges(" ".join(cleaned.split()))
    if re.match(r"^stuff\b", cleaned, re.I):
        cleaned = "Assorted curbside items" if ADDRESS_CUE.search(source) else "Assorted items"
    elif re.match(r"^random\s+", cleaned, re.I):
        cleaned = re.sub(r"^random\b", "Assorted", cleaned, count=1, flags=re.I)
    elif re.match(r"^(?:@|at)\s+(?:the\s+)?curb\b", cleaned, re.I):
        cleaned = "Curbside items"
    elif re.match(r"^misc(?:ellaneous)?\b", cleaned, re.I):
        cleaned = "Assorted items"
    elif re.search(r"\bnordictrack\b.*\bachiever\b.*\bski\s+machine\b", cleaned, re.I):
        # Put the model and actual product first; keep the repair caveat.
        cleaned = ("NordicTrack Achiever ski machine" +
                   (" (parts/repair)" if re.search(r"\bparts\s*/\s*repair\b", cleaned, re.I)
                    else ""))
    elif (re.fullmatch(r"(?:it\s+)?works?\s+(?:perfectly|great|fine|well)[.!]?",
                       cleaned, re.I) or
          re.fullmatch(r"\$?\d+(?:[.,]\d{1,2})?", cleaned)):
        cleaned = ""
    count_noun = re.fullmatch(r"([2-9]|\d{2,})\s+(cabinet|box|chair|table)",
                              cleaned, re.I)
    if count_noun:
        count, noun = count_noun.groups()
        cleaned = f"{count} {'boxes' if noun.casefold() == 'box' else noun + 's'}"
    if not cleaned:
        cleaned = str(item_type or "").replace("_", " ").strip()
    if not cleaned:
        return "Unidentified item"
    if cleaned.isupper() and len(cleaned) > 3:
        cleaned = " ".join(
            word.upper() if (any(char.isdigit() for char in word) or "+" in word or
                             TITLE_ACRONYMS.fullmatch(word)) else word.lower()
            for word in cleaned.split())
    cleaned = re.sub(r"^free people\b", "Free People", cleaned, flags=re.I)
    return cleaned[0].upper() + cleaned[1:]


def _photo_sentence(observations: dict) -> str:
    evidence = next((point[6:].strip() for point in
                     observations.get("classification_evidence") or []
                     if isinstance(point, str) and point.startswith("Photo:")), "")
    if not evidence:
        item = str(observations.get("item_type") or
                   observations.get("primary_object") or "item").replace("_", " ")
        return f"The saved photo appears to show a {item}."
    sentence = evidence.split(". ", 1)[0].strip()
    sentence = re.sub(r"\bclearly\s+", "", sentence, flags=re.I)
    sentence = re.sub(r"^The (?:main object in the photo|primary object|item) is\s+",
                      "The photo shows ", sentence, flags=re.I)
    sentence = re.sub(r",?\s+which is (?:the item being offered|consistent with .*?)\.?$",
                      "", sentence, flags=re.I)
    sentence = re.sub(r",?\s+matching the description\.?$", "", sentence,
                      flags=re.I)
    sentence = re.sub(r"\bfree\b", "", sentence, flags=re.I)
    sentence = " ".join(sentence.split()).strip(" ,.;")
    return sentence + "." if sentence else "The saved photo needs review."


def grounded_description(listing: dict) -> str:
    """Use model-written photo evidence and a directly quoted repair claim."""
    observations = (listing.get("vision") or {}).get("observations") or {}
    sentence = _photo_sentence(observations)
    condition = ((listing.get("condition_assessment") or {}).get("result") or {})
    if condition.get("condition_grade") == "needs_repair":
        claim = next((point.split(":", 1)[1].strip() for point in
                      condition.get("evidence") or []
                      if isinstance(point, str) and
                      point.startswith("Seller description:") and
                      REPAIR_CUE.search(point)), None)
        if claim:
            claim = FREE_PREFIX.sub("", claim).strip(" .;!")
            sentence += f" Seller says: {claim}."
    name = str(listing.get("display_title") or "").casefold()
    if (name and re.search(r"\bonly the\s+" + re.escape(name) + r"\b",
                           str(listing.get("description") or ""), re.I)):
        sentence += f" Seller says only the {name} is included."
    return sentence


def mixed_lot_summary(description: str) -> str:
    seller_text = " ".join(str(description or "").split())
    first_item = LOT_NOUNS.search(seller_text)
    if first_item:
        seller_text = seller_text[first_item.start():]
    return ("Seller lists: " + seller_text[:220].rstrip(" ,.;") +
            ". Conditions may differ between items.")


def run(input_file: Path, output_file: Path) -> dict:
    data = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("listings"), list):
        raise ValueError("Input must have a listings array")
    for listing in data["listings"]:
        observations = (listing.get("vision") or {}).get("observations") or {}
        listing["mixed_lot"] = mixed_item_lot(
            listing.get("title") or "", listing.get("description") or "")
        listing["display_title"] = display_title(
            listing.get("title") or "", listing.get("asking_price"),
            observations.get("item_type") or "", listing.get("description") or "")
        if listing["mixed_lot"]:
            listing["generated_description"] = mixed_lot_summary(
                listing.get("description") or "")
            listing["generated_description_status"] = "seller_list"
            listing["generated_description_model"] = None
        elif observations.get("listing_kind") == "product":
            listing["generated_description"] = grounded_description(listing)
            listing["generated_description_status"] = "generated"
            listing["generated_description_model"] = (listing.get("vision") or {}).get("model")
        else:
            listing["generated_description"] = None
            listing["generated_description_status"] = "not_applicable"
    data["generated_description_method"] = COPY_VERSION
    data["generated_description_processed"] = len(data["listings"])
    data["generated_description_at"] = datetime.now(timezone.utc).isoformat()
    output_file.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_file, data)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Build grounded card descriptions")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.input, args.output)
    print(json.dumps({"processed": result["generated_description_processed"],
                      "output": str(args.output.resolve())}, indent=2))


if __name__ == "__main__":
    main()
