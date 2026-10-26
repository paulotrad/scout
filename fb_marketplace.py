"""Collect Marketplace card text and thumbnails over a direct connection."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit

import requests

from marketplace_browser import create_driver
from marketplace_db import DEFAULT_DB, store_result
from marketplace_title import repair_price_only_title
from marketplace_value import (attach_identity_and_value, furniture_signature,
                               identify_item, infer_condition)


ITEM_URL = re.compile(r"/marketplace/item/(\d+)")
PRICE = re.compile(r"^\$\s*([\d,]+(?:\.\d{1,2})?)\b")
LOCATION = re.compile(r"^.{2,70},\s*[A-Z]{2}$")
SKIP_LINES = {"sponsored", "new listing", "shipping available", "local pickup"}
_LOCAL_SETTINGS = Path(__file__).with_name("marketplace_settings.json")
DEFAULT_SETTINGS = (_LOCAL_SETTINGS if _LOCAL_SETTINGS.is_file() else
                    Path(__file__).with_name("marketplace_settings.example.json"))


def _marketplace_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "facebook.com", "www.facebook.com", "web.facebook.com"
    } or not parsed.path.startswith("/marketplace/"):
        raise ValueError("Provide an https://www.facebook.com/marketplace/... URL")
    return url


def parse_card(raw: dict) -> dict | None:
    """Parse price, title and location from one visible Marketplace link."""
    url = raw.get("url") or raw.get("href") or ""
    match = ITEM_URL.search(url)
    if not match:
        return None
    lines = [re.sub(r"\s+", " ", part).strip()
             for part in (raw.get("text") or "").splitlines()]
    lines = [line for line in lines if line and line.casefold() not in SKIP_LINES]
    if not lines:
        return None
    price_index = next((index for index, line in enumerate(lines)
                        if PRICE.match(line) or line.casefold() == "free"), None)
    if price_index is None:
        return None
    price_line = lines[price_index]
    price_match = PRICE.match(price_line)
    price = float(price_match.group(1).replace(",", "")) if price_match else 0.0
    after_price = lines[price_index + 1:]
    title = next((line for line in after_price if not LOCATION.match(line)), None)
    if not title:
        return None
    location = next((line for line in after_price if LOCATION.match(line)), None)
    # A seller can put a price-like phrase on the same line as the asking price.
    if price_match and price_match.end() < len(price_line):
        remainder = price_line[price_match.end():].strip(" -·")
        if remainder:
            title = remainder
    parsed = {
        "listing_id": match.group(1),
        "url": "https://www.facebook.com/marketplace/item/" + match.group(1) + "/",
        "asking_price": price,
        "currency": "USD" if price_match else None,
        "title": title,
        "title_truncated": title.endswith(("...", "…")),
        "location": location,
        "location_verified": False,
        "raw_card_text": lines,
    }
    if raw.get("image_url"):
        parsed["_image_url"] = raw["image_url"]
    repair_price_only_title(parsed)
    return parsed


def visible_cards(driver) -> list[dict]:
    """Extract text and displayed photo URLs from visible listing links."""
    return driver.execute_script("""
        const visible = el => {
          const style = getComputedStyle(el);
          return style.display !== 'none' && style.visibility !== 'hidden' &&
                 el.getClientRects().length > 0;
        };
        return [...document.querySelectorAll('a[href*="/marketplace/item/"]')]
          .filter(visible).map(anchor => {
            let node = anchor;
            let best = (anchor.innerText || '').trim();
            for (let step = 0; step < 3 && node.parentElement; step++) {
              node = node.parentElement;
              const candidate = (node.innerText || '').trim();
              if (candidate.length > best.length && candidate.length < 450 &&
                  candidate.split('\\n').length <= 12) best = candidate;
              if (best.split('\\n').length >= 3) break;
            }
            const image = anchor.querySelector('img');
            return {url: anchor.href, text: best,
                    image_url: image ? (image.currentSrc || image.src) : null};
          });
    """)


def save_listing_images(listings: list[dict], output: Path) -> int:
    """Save only selected card thumbnails from Facebook's CDN, without proxies."""
    image_dir = output.parent / f"{output.stem}_images"
    image_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.trust_env = False
    session.proxies.clear()
    saved = 0
    extensions = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    signatures = {
        "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
        "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    try:
        for listing in listings:
            image_url = listing.pop("_image_url", None)
            listing["image_file"] = None
            listing["image_status"] = "not_available"
            if not image_url:
                continue
            parsed = urlsplit(image_url)
            if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".fbcdn.net"):
                listing["image_status"] = "invalid_source"
                continue
            try:
                with session.get(image_url, timeout=15, stream=True) as response:
                    response.raise_for_status()
                    final_host = urlsplit(response.url).hostname or ""
                    if not final_host.endswith(".fbcdn.net"):
                        raise ValueError("Image redirected outside Facebook CDN")
                    media_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    if media_type not in extensions:
                        raise ValueError("Response was not a supported image")
                    content = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        content.extend(chunk)
                        if len(content) > 8 * 1024 * 1024:
                            raise ValueError("Image exceeded 8 MiB limit")
                    if not signatures[media_type](content):
                        raise ValueError("Response had an invalid image signature")
                image_path = image_dir / f"{listing['listing_id']}{extensions[media_type]}"
                image_path.write_bytes(content)
                listing["image_file"] = str(image_path.relative_to(output.parent))
                listing["image_status"] = "saved"
                listing["image_bytes"] = len(content)
                saved += 1
            except (requests.RequestException, OSError, ValueError):
                listing["image_status"] = "download_failed"
    finally:
        session.close()
    return saved


def close_optional_see_more_dialog(driver) -> bool:
    """Dismiss Facebook's public-page dialog using its visible Close control."""
    from selenium.webdriver.common.by import By
    for dialog in driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]'):
        if not dialog.is_displayed() or "See more on Facebook" not in dialog.text:
            continue
        close = dialog.find_elements(By.CSS_SELECTOR, '[aria-label="Close"]')
        if close and close[0].is_displayed():
            close[0].click()
            return True
    return False


def set_location_zip(driver, zip_code: str, radius_miles: int) -> str:
    """Use the visible Marketplace location picker and confirm a selected place.

    Facebook changes its UI. If no picker or selected suggestion can be found,
    fail instead of silently treating the configured ZIP as active.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.common.exceptions import ElementClickInterceptedException, TimeoutException
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, 15, poll_frequency=0.25)
    sidebar = [element for element in driver.find_elements(
        By.CSS_SELECTOR, '[aria-label^="Location:"][role="button"]')
        if element.is_displayed() and element.is_enabled()]
    picker = sidebar[0] if sidebar else None
    picker_xpaths = (
        "//*[@role='button' and contains(translate(@aria-label, 'LOCATION', 'location'), 'location')]",
        "//button[contains(translate(@aria-label, 'LOCATION', 'location'), 'location')]",
        "//*[contains(text(), 'Location')]/ancestor::*[@role='button'][1]",
    )
    if not picker:
        for xpath in picker_xpaths:
            for element in driver.find_elements(By.XPATH, xpath):
                if element.is_displayed() and element.is_enabled():
                    picker = element
                    break
            if picker:
                break
    if not picker:
        labels = driver.find_elements(By.CSS_SELECTOR, '[aria-label^="Location:"]')
        current_place = labels[0].get_attribute("aria-label").split(":", 1)[1].strip() if labels else ""
        if current_place:
            picker = next((element for element in driver.find_elements(By.CSS_SELECTOR, '[role="button"]')
                           if element.is_displayed() and element.is_enabled() and
                           element.text.strip() == current_place), None)
    if not picker:
        raise RuntimeError("Could not find Marketplace's location picker; the ZIP was not applied")
    initial_place = picker.get_attribute("aria-label") or picker.text.strip()
    for attempt in range(5):
        try:
            picker.click()
            break
        except ElementClickInterceptedException as error:
            if attempt == 4:
                raise RuntimeError("Location click intercepted by another visible layer") from error
            time.sleep(0.7)

    field = wait.until(lambda browser: next((element for element in browser.find_elements(
        By.CSS_SELECTOR,
        'input[placeholder*="location" i], input[placeholder*="city" i], '
        'input[placeholder*="zip" i], input[aria-label*="location" i]')
        if element.is_displayed()), None))
    # React's controlled input can keep the old city after WebElement.clear().
    field.click()
    field.send_keys(Keys.CONTROL, "a")
    field.send_keys(Keys.BACKSPACE)
    field.send_keys(zip_code)
    if field.get_attribute("value") != zip_code:
        raise RuntimeError("Marketplace location input did not contain only the requested ZIP")
    try:
        suggestion = wait.until(lambda browser: next((element for element in browser.find_elements(
            By.XPATH, f"//*[contains(text(), '{zip_code}')]/ancestor::*[@role='option' or @role='button'][1]"
                      f" | //*[@role='option' and contains(., '{zip_code}')]")
            if element.is_displayed() and element != field), None))
    except TimeoutException as error:
        raise RuntimeError("Marketplace did not offer a selectable result for the ZIP; location was not applied") from error
    selected_place = suggestion.text.strip().splitlines()[0]
    suggestion.click()
    if not selected_place:
        raise RuntimeError("Marketplace location suggestion had no readable place name")

    # Facebook currently uses a combobox with discrete radii (10 and 20 miles,
    # but no 15). Select the smallest offered radius covering the request.
    radius_controls = [element for element in driver.find_elements(
        By.CSS_SELECTOR, '[role="combobox"]')
        if element.is_displayed() and "Radius" in element.text]
    if radius_controls:
        radius_control = radius_controls[0]
        radius_control.click()
        choices = []
        for option in driver.find_elements(By.CSS_SELECTOR, '[role="option"]'):
            if option.is_displayed() and (match := re.fullmatch(
                    r"(\d+)\s+miles?", option.text.strip(), re.I)):
                choices.append((int(match.group(1)), option))
        covering = [(miles, option) for miles, option in choices if miles >= radius_miles]
        if not covering:
            raise RuntimeError("Marketplace offered no radius covering the request")
        selected_miles, selected_option = min(covering, key=lambda row: row[0])
        selected_option.click()
        wait.until(lambda browser: any(
            element.is_displayed() and re.search(rf"\b{selected_miles}\s+miles?\b",
                                               element.text, re.I)
            for element in browser.find_elements(By.CSS_SELECTOR, '[role="combobox"]')
            if "Radius" in element.text))
    else:
        distance = driver.find_elements(By.CSS_SELECTOR, 'input[type="range"]')
        if distance:
            driver.execute_script("""
                const input = arguments[0], requested = arguments[1];
                const setter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype, 'value').set;
                setter.call(input, Math.max(Number(input.min || 1),
                    Math.min(Number(input.max || 500), requested)));
                input.dispatchEvent(new Event('input', {bubbles:true}));
                input.dispatchEvent(new Event('change', {bubbles:true}));
            """, distance[0], radius_miles)
    apply = wait.until(lambda browser: next((element for element in browser.find_elements(
        By.XPATH, "//button[normalize-space()='Apply'] | //*[@role='button' and normalize-space()='Apply']")
        if element.is_displayed() and element.is_enabled()), None))
    apply.click()
    # Facebook can normalize a ZIP suggestion to a nearby place name in its
    # sidebar (21009 currently appears as Belcamp after selecting Box Hill
    # South). Check that Apply closed the picker, then record the actual label.
    def applied_location(browser):
        if any(dialog.is_displayed() and "Change location" in dialog.text
               for dialog in browser.find_elements(By.CSS_SELECTOR, '[role="dialog"]')):
            return False
        actual_place = browser.execute_script("""
            const label = [...document.querySelectorAll('[aria-label^="Location:"]')]
              .find(el => el.getClientRects().length);
            return label ? label.getAttribute('aria-label').split(':').slice(1).join(':').trim() : false;
        """)
        if not actual_place:
            return False
        if actual_place in initial_place:
            return False
        return actual_place

    actual_place = wait.until(applied_location)
    if not actual_place:
        raise RuntimeError("Marketplace did not show an applied location")
    return actual_place


def load_settings(path: Path, zip_override: str | None = None,
                  keywords_override: list[str] | None = None,
                  radius_override: int | None = None) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Marketplace settings must be an object")
    zip_code = zip_override or raw.get("zip_code")
    if not isinstance(zip_code, str) or not re.fullmatch(r"\d{5}", zip_code):
        raise ValueError("zip_code must contain five digits")
    keywords = keywords_override if keywords_override else raw.get("keywords", ["free"])
    if not isinstance(keywords, list) or not keywords or not all(
        isinstance(word, str) and 0 < len(word.strip()) <= 80 for word in keywords
    ):
        raise ValueError("keywords must be a nonempty list of short strings")
    radius = radius_override if radius_override is not None else raw.get("radius_miles", 40)
    if not isinstance(radius, int) or not 1 <= radius <= 500:
        raise ValueError("radius_miles must be an integer from 1 to 500")
    return {"zip_code": zip_code, "keywords": list(dict.fromkeys(
            word.strip() for word in keywords)), "radius_miles": radius}


def confirmed_search_place(driver, selected_place: str) -> str:
    """Require the location label to persist after search navigation."""
    from selenium.webdriver.support.ui import WebDriverWait

    city = selected_place.casefold().split(",")[0].strip()

    def matching_label(browser):
        return browser.execute_script("""
            const city = arguments[0];
            const label = [...document.querySelectorAll('[aria-label^="Location:"]')]
              .filter(el => el.getClientRects().length)
              .map(el => (el.getAttribute('aria-label') || '').split(':').slice(1).join(':').trim())
              .find(text => text.toLowerCase().includes(city));
            return label || false;
        """, city)

    try:
        return WebDriverWait(driver, 12, poll_frequency=0.3).until(matching_label)
    except Exception as error:
        raise RuntimeError("Marketplace did not keep the selected location for this search") from error


def collect_from_page(driver, max_items: int, scrolls: int, login_wait: int,
                      until_stale: bool = False, stats: dict | None = None) -> list[dict]:
    """Scroll until capped or no new cards appear on five consecutive passes."""
    deadline = time.monotonic() + login_wait
    while True:
        cards = visible_cards(driver)
        if cards:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("No visible Marketplace cards. Log in in the browser, or use a listings URL accessible to this session.")
        time.sleep(1)
    by_id = {}
    stale_passes = 0
    stop_reason = "scroll_limit"
    for scroll in range(scrolls + 1):
        count_before = len(by_id)
        for raw in cards:
            parsed = parse_card(raw)
            if parsed:
                by_id.setdefault(parsed["listing_id"], parsed)
            if len(by_id) >= max_items:
                break
        if len(by_id) >= max_items:
            stop_reason = "item_limit"
            break
        if until_stale:
            stale_passes = stale_passes + 1 if len(by_id) == count_before else 0
            if stale_passes >= 5:
                stop_reason = "no_new_cards_after_five_scrolls"
                break
        if scroll == scrolls:
            break
        if until_stale and scroll and scroll % 10 == 0:
            print(f"Scrolled {scroll} times; {len(by_id)} unique cards so far", flush=True)
        driver.execute_script("""
            const anchor = document.querySelector('a[href*="/marketplace/item/"]');
            let node = anchor;
            while (node) {
              const overflow = getComputedStyle(node).overflowY;
              if (node.scrollHeight > node.clientHeight + 50 &&
                  (overflow === 'auto' || overflow === 'scroll')) {
                node.scrollBy(0, Math.round(node.clientHeight * 0.85));
                return;
              }
              node = node.parentElement;
            }
            window.scrollBy(0, Math.round(window.innerHeight * 0.85));
        """)
        time.sleep(1.2)
        cards = visible_cards(driver)
    if stats is not None:
        stats.update({"scrolls_completed": scroll, "stop_reason": stop_reason,
                      "unique_cards": len(by_id)})
    return list(by_id.values())[:max_items]


def _read_list(path: Path | None) -> list[dict]:
    if path is None:
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ValueError(f"Expected an array of objects in {path}")
    return data


def keyword_routes(location_base: str, keyword: str,
                   include_free_search: bool = False) -> list[tuple[str, str]]:
    if keyword.casefold() == "free":
        routes = [("free_category", location_base + "free/")]
        if include_free_search:
            routes.append(("free_text", location_base + "search/?query=free"))
        return routes
    return [(keyword, location_base + "search/?query=" + quote(keyword))]


def _comp_searches(driver, listings: list[dict], catalog: list[dict],
                   count: int, max_items: int, selected_place: str,
                   location_base: str) -> list[dict]:
    """Collect nearby exact-item or same-form furniture asking prices."""
    selected = {}
    for listing in listings:
        identity = identify_item(listing["title"], catalog)
        if identity["id"] and identity["confidence"] >= 0.75:
            selected.setdefault(("item", identity["id"]), identity["name"])
        elif signature := furniture_signature(listing["title"]):
            key = ("furniture", signature["product_type"],
                   tuple(sorted(signature["material_clues"])))
            words = [material.replace("_", " ") for material in key[2]]
            selected.setdefault(key, " ".join([*words, signature["product_type"]]))
    comps = []
    for key, name in list(selected.items())[:count]:
        driver.get(location_base + "search/?query=" + quote(name))
        try:
            confirmed_search_place(driver, selected_place)
            results = collect_from_page(driver, max_items, 1, 12)
        except RuntimeError:
            continue
        for row in results:
            if key[0] == "item":
                identity = identify_item(row["title"], catalog)
                if identity["id"] != key[1] or identity["confidence"] < 0.75:
                    continue
            else:
                signature = furniture_signature(row["title"])
                if (not signature or signature["product_type"] != key[1] or
                        not set(key[2]) <= set(signature["material_clues"])):
                    continue
            comps.append({"source": "facebook_marketplace",
                          "listing_id": row["listing_id"],
                          "item_id": key[1] if key[0] == "item" else None,
                          "asking_price": row["asking_price"],
                          "condition": infer_condition(row["title"])["grade"],
                          "title": row["title"],
                          "location": row["location"], "source_url": row["url"],
                          "search_zip": listings[0].get("search_zip")})
    return comps


def run(url: str | None, fixture: Path | None, settings: dict,
        catalog_path: Path | None,
        comps_path: Path | None, output: Path, max_items: int, scrolls: int,
        login_wait: int, headless: bool, comp_searches: int,
        profile_dir: Path | None = None, db_path: Path | None = None,
        until_stale: bool = False, include_free_search: bool = False) -> dict:
    if max_items < 1 or scrolls < 0 or login_wait < 0 or comp_searches < 0:
        raise ValueError("Counts and timeouts must be nonnegative; max-items must be positive")
    catalog = _read_list(catalog_path)
    comps = _read_list(comps_path)
    collection_stats = {}
    if fixture:
        listings = [parsed for raw in _read_list(fixture)
                    if (parsed := parse_card(raw)) is not None][:max_items]
        for listing in listings:
            listing["search_zip"] = settings["zip_code"]
        source = "local_fixture"
        selected_place = None
        image_count = 0
    else:
        url = _marketplace_url(url or "https://www.facebook.com/marketplace/")
        driver, cleanup = create_driver(headless=headless, profile_dir=profile_dir)
        try:
            driver.set_window_size(1920, 1080)
            driver.get(url)
            close_optional_see_more_dialog(driver)
            try:
                selected_place = set_location_zip(
                    driver, settings["zip_code"], settings["radius_miles"])
            except RuntimeError as error:
                if not login_wait or "click intercepted" not in str(error):
                    raise
                if headless:
                    print("A temporary page layer covers Location; retrying the sidebar control.", flush=True)
                else:
                    print("Another layer covers Location. Close it in Chrome; retrying the sidebar control.", flush=True)
                deadline = time.monotonic() + login_wait
                while True:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Location was still covered after --login-wait expired") from error
                    time.sleep(2)
                    close_optional_see_more_dialog(driver)
                    try:
                        selected_place = set_location_zip(
                            driver, settings["zip_code"], settings["radius_miles"])
                        break
                    except RuntimeError as retry_error:
                        if "click intercepted" not in str(retry_error):
                            raise
            location_match = re.search(r"/marketplace/(\d+)/", urlsplit(driver.current_url).path)
            if not location_match:
                raise RuntimeError("Marketplace did not expose a location-specific route after Apply")
            location_base = ("https://www.facebook.com/marketplace/" +
                             location_match.group(1) + "/")
            listings_by_id = {}
            for keyword in settings["keywords"]:
                for mode, search_url in keyword_routes(location_base, keyword,
                                                       include_free_search):
                    driver.get(search_url)
                    confirmed_place = confirmed_search_place(driver, selected_place)
                    keyword_stats = {}
                    for listing in collect_from_page(driver, max_items, scrolls, 15,
                                                     until_stale, keyword_stats):
                        listing["search_keyword"] = keyword
                        listing["search_zip"] = settings["zip_code"]
                        listing["selected_search_place"] = confirmed_place
                        existing = listings_by_id.get(listing["listing_id"])
                        if existing:
                            if mode not in existing["search_modes"]:
                                existing["search_modes"].append(mode)
                        else:
                            listing["search_modes"] = [mode]
                            listings_by_id[listing["listing_id"]] = listing
                    collection_stats[mode] = keyword_stats
            listings = list(listings_by_id.values())
            image_count = save_listing_images(listings, output)
            if comp_searches:
                comps.extend(_comp_searches(driver, listings, catalog, comp_searches,
                                            max_items, selected_place, location_base))
        finally:
            cleanup()
        source = url
    enriched = attach_identity_and_value(listings, catalog, comps)
    result = {
        "source": source,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "requested_zip": settings["zip_code"],
        "selected_place": selected_place,
        "search_keywords": settings["keywords"],
        "radius_miles_requested": settings["radius_miles"],
        "radius_miles_applied": (int(match.group(1)) if selected_place and
                                  (match := re.search(r"Within (\d+) mi", selected_place))
                                  else None),
        "network_route": "direct",
        "images_blocked": False,
        "images_downloaded": image_count,
        "listing_count": len(enriched),
        "identified_count": sum(bool(row["identity"]["id"]) for row in enriched),
        "valued_count": sum(row["valuation"]["status"] in {"estimated", "provisional"}
                            for row in enriched),
        "comparison_searches": comp_searches if not fixture else 0,
        "collection_stats": collection_stats,
        "comparables": comps,
        "listings": enriched,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    if db_path is not None:
        stored = store_result(result, output.parent, db_path)
        result["database_file"] = stored["database_file"]
        result["database_images_stored"] = stored["images_stored"]
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Text-first Facebook Marketplace Selenium collector")
    parser.add_argument("--url", help="Facebook Marketplace page visible to your browser session")
    parser.add_argument("--fixture", type=Path, help="Offline array of raw card text and URLs")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS,
                        help="JSON with zip_code, radius_miles, and saved keywords")
    parser.add_argument("--zip-code", help="Override the saved five-digit ZIP for this run")
    parser.add_argument("--radius-miles", type=int,
                        help="Override the saved Marketplace search radius for this run")
    parser.add_argument("--keyword", action="append", dest="keywords",
                        help="Override saved keywords; repeat for more searches")
    parser.add_argument("--catalog", type=Path, help="Optional canonical item aliases JSON")
    parser.add_argument("--comps", type=Path,
                        help="Optional local Facebook/Craigslist asking-price JSON; Craigslist data is supplied by you")
    parser.add_argument("--output", type=Path, default=Path("runs/facebook_marketplace.json"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="SQLite database for listings, price history, and image BLOBs")
    parser.add_argument("--max-items", type=int, default=40)
    parser.add_argument("--scrolls", type=int, default=3)
    parser.add_argument("--until-stale", action="store_true",
                        help="Stop after five scroll passes without new cards, up to --scrolls")
    parser.add_argument("--include-free-search", action="store_true",
                        help="Also search the word free and combine distinct cards with Free Stuff")
    parser.add_argument("--login-wait", type=int, default=120,
                        help="Seconds to allow manual login in the visible browser")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--profile-dir", type=Path,
                        help="Optional persistent Chrome profile for a manual Facebook login")
    parser.add_argument("--comp-searches", type=int, default=0,
                        help="Search exact items or furniture forms nearby in the same Facebook session")
    args = parser.parse_args()
    settings = load_settings(args.settings, args.zip_code, args.keywords,
                             args.radius_miles)
    result = run(args.url, args.fixture, settings, args.catalog, args.comps, args.output,
                 args.max_items, args.scrolls, args.login_wait, args.headless,
                 args.comp_searches, args.profile_dir, args.db, args.until_stale,
                 args.include_free_search)
    print(json.dumps({key: value for key, value in result.items() if key != "listings"}, indent=2))
    print(f"Saved listings to {args.output.resolve()}")


if __name__ == "__main__":
    main()
