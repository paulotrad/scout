"""Create card copy from saved 8B vision evidence without adding new claims."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re


COPY_VERSION = "grounded_photo_description_v1"
FREE_PREFIX = re.compile(r"^free\b[\s:!.,;\-–—]*", re.I)
REPAIR_CUE = re.compile(
    r"\b(?:broken|not working|stopped working|no longer|does not|doesn't|"
    r"needs? repair|needs? a new|missing|damaged)\b", re.I)


def display_title(title: str, asking_price: object, item_type: str = "") -> str:
    """Remove a leading Free marker when the listed price is zero."""
    source = str(title or "").strip()
    if asking_price != 0:
        return source
    cleaned = FREE_PREFIX.sub("", source, count=1).strip(" \t\r\n!.,;:-–—")
    if not cleaned:
        cleaned = str(item_type or "").replace("_", " ").strip()
    return cleaned[0].upper() + cleaned[1:] if cleaned else source


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


def run(input_file: Path, output_file: Path) -> dict:
    data = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("listings"), list):
        raise ValueError("Input must have a listings array")
    for listing in data["listings"]:
        observations = (listing.get("vision") or {}).get("observations") or {}
        listing["display_title"] = display_title(
            listing.get("title") or "", listing.get("asking_price"),
            observations.get("item_type") or "")
        if observations.get("listing_kind") == "product":
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
    temporary = output_file.with_suffix(output_file.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(output_file)
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
