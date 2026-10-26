"""Read visible Marketplace item descriptions through direct Selenium pages."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

from selenium.webdriver.support.ui import WebDriverWait

from marketplace_browser import create_driver
from fb_marketplace import _marketplace_url
from marketplace_db import DEFAULT_DB, store_result


DETAIL_VERSION = "visible_details_v1"
CONDITION_LINE = re.compile(r"^(?:Used\s*-\s*(?:Like New|Good|Fair)|New|For parts)$", re.I)
STOP_LINES = {"Seller information", "Related searches", "Today's picks",
              "Message", "See more on Facebook"}


def extract_description(body_text: str) -> dict:
    """Keep seller text from Details, stopping before location and seller profile."""
    lines = [re.sub(r"\s+", " ", line).strip()
             for line in body_text.splitlines()]
    lines = [line for line in lines if line]
    try:
        start = lines.index("Details") + 1
    except ValueError:
        return {"description": None, "seller_condition_label": None,
                "description_status": "details_not_visible"}
    condition = None
    description_lines = []
    section = lines[start:]
    for index, line in enumerate(section):
        if (line in STOP_LINES or "Location is approximate" in line or
                line == "Seller details"):
            break
        if line == "Condition":
            if index + 1 < len(section) and CONDITION_LINE.match(section[index + 1]):
                condition = section[index + 1]
            continue
        if CONDITION_LINE.match(line) and index > 0 and section[index - 1] == "Condition":
            continue
        description_lines.append(line)
    description = "\n".join(description_lines).strip() or None
    truncated = bool(description and len(description) > 4000)
    if truncated:
        description = description[:4000]
    return {"description": description, "seller_condition_label": condition,
            "description_status": "visible" if description else "no_visible_description",
            "description_truncated": truncated}


def fetch_details(driver, listing: dict, timeout_seconds: int = 12) -> dict:
    url = _marketplace_url(listing["url"])
    driver.get(url)
    try:
        WebDriverWait(driver, timeout_seconds, poll_frequency=0.4).until(
            lambda browser: "Details" in (browser.execute_script(
                "return document.body ? document.body.innerText : ''") or ""))
    except Exception:
        return {"description": None, "seller_condition_label": None,
                "description_status": "details_not_visible"}
    time.sleep(0.3)
    body = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    return extract_description(body)


def enrich_run(input_file: Path, output_file: Path, *, limit: int | None = None,
               resume: bool = False, headless: bool = True,
               db_path: Path | None = DEFAULT_DB) -> dict:
    data = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("listings"), list):
        raise ValueError("Input must be a Marketplace result with a listings array")
    if limit is not None and limit < 1:
        raise ValueError("Limit must be positive")
    previous = {}
    if resume and output_file.is_file():
        prior = json.loads(output_file.read_text(encoding="utf-8"))
        if (prior.get("detail_version") == DETAIL_VERSION and
                prior.get("captured_at") == data.get("captured_at")):
            previous = {str(row.get("listing_id")): row for row in prior["listings"]
                        if isinstance(row, dict) and row.get("listing_id")}
    selected = data["listings"][:limit]
    output_file.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint() -> None:
        data["detail_version"] = DETAIL_VERSION
        data["details_processed"] = sum(bool(row.get("description_status"))
                                        for row in selected)
        data["descriptions_visible"] = sum(bool(row.get("description"))
                                           for row in selected)
        data["details_at"] = datetime.now(timezone.utc).isoformat()
        temporary = output_file.with_suffix(output_file.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        temporary.replace(output_file)

    driver, cleanup = create_driver(headless=headless)
    try:
        driver.set_window_size(1920, 1080)
        for index, listing in enumerate(selected, 1):
            prior = previous.get(str(listing.get("listing_id"))) or {}
            if prior.get("description_status") in {"visible", "no_visible_description"}:
                for key in ("description", "seller_condition_label",
                            "description_status", "description_truncated",
                            "detail_captured_at"):
                    if key in prior:
                        listing[key] = prior[key]
                print(f"{index}/{len(selected)} reused {listing.get('listing_id')}", flush=True)
                checkpoint()
                continue
            try:
                details = fetch_details(driver, listing)
            except Exception as error:
                details = {"description": None, "seller_condition_label": None,
                           "description_status": "error",
                           "detail_error": str(error)[:200]}
            listing.update(details)
            listing["detail_captured_at"] = datetime.now(timezone.utc).isoformat()
            print(f"{index}/{len(selected)} {listing['description_status']} "
                  f"{listing.get('listing_id')}", flush=True)
            checkpoint()
    finally:
        cleanup()
    if db_path is not None:
        stored = store_result(data, input_file.parent, db_path)
        data["database_file"] = stored["database_file"]
        checkpoint()
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Read visible Marketplace descriptions")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--visible", action="store_true", help="Show Chrome while reading details")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    result = enrich_run(args.input, args.output, limit=args.limit,
                        resume=args.resume, headless=not args.visible,
                        db_path=args.db)
    print(json.dumps({"details_processed": result["details_processed"],
                      "descriptions_visible": result["descriptions_visible"],
                      "output": str(args.output.resolve())}, indent=2))


if __name__ == "__main__":
    main()
