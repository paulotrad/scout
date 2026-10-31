"""Small, text-only Craigslist asking-price search for human review.

One HTML search page is requested. Images and listing detail pages are never
requested, and search results are not automatically approved as comparables.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from threading import Lock
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


MAX_HTML_BYTES = 600_000
MAX_REQUESTS_PER_HOUR = 24
CACHE_SECONDS = 60 * 60
_lock = Lock()
_cache: dict[tuple[str, str, int], tuple[float, dict]] = {}
_requests: deque[float] = deque()
_excluded = re.compile(r"\b(?:wanted|looking to buy|cash paid|repair service|pickup service)\b", re.I)
_price = re.compile(r"\$\s*(\d[\d,]*(?:\.\d{1,2})?)")
_search_stopwords = {"free", "used", "new", "mens", "womens", "with", "for",
                     "sale", "the", "and", "inch", "pair", "only"}


class SearchUnavailable(Exception):
    pass


class SearchLimitReached(Exception):
    pass


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self.current: dict | None = None
        self.field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "li" and "cl-static-search-result" in classes:
            self.current = {"title": "", "price_text": "", "location": "", "url": ""}
            self.field = None
        elif self.current is not None:
            if tag == "a" and not self.current["url"]:
                self.current["url"] = attributes.get("href") or ""
            elif tag in {"div", "span"}:
                self.field = next((name for name in ("title", "price", "location")
                                   if name in classes), None)

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.field:
            key = "price_text" if self.field == "price" else self.field
            self.current[key] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self.current is not None:
            self.rows.append(self.current)
            self.current = None
            self.field = None
        elif tag in {"div", "span"} and self.current is not None:
            self.field = None


def parse_search_html(source: str, *, limit: int = 12) -> list[dict]:
    parser = _SearchParser()
    parser.feed(source)
    results = []
    seen = set()
    for row in parser.rows:
        title = " ".join(row["title"].split())
        place = " ".join(row["location"].split())
        url = row["url"]
        host = urlsplit(url).hostname or ""
        price_match = _price.search(row["price_text"])
        if (not title or _excluded.search(title) or not price_match or
                not (host == "craigslist.org" or host.endswith(".craigslist.org")) or
                url in seen):
            continue
        asking_price = float(price_match.group(1).replace(",", ""))
        if not 2 <= asking_price <= 1_000_000:
            continue
        seen.add(url)
        results.append({"title": title[:180], "asking_price": asking_price,
                        "location": place[:100], "url": url})
        if len(results) >= limit:
            break
    return results


def _matches_query(title: str, query: str) -> bool:
    tokens = {word for word in re.findall(r"[a-z0-9]+", query.casefold())
              if len(word) >= 3 and word not in _search_stopwords}
    found = set(re.findall(r"[a-z0-9]+", title.casefold()))
    return len(tokens & found) >= min(2, len(tokens)) if tokens else False


def search_craigslist(query: str, zip_code: str, radius_miles: int) -> dict:
    """Return unreviewed asking-price cards from one public search page."""
    query = " ".join(query.split())[:100]
    if not query or not re.fullmatch(r"\d{5}", zip_code) or not 1 <= radius_miles <= 1000:
        raise ValueError("Valid item, ZIP code, and radius required")
    key = (query.casefold(), zip_code, radius_miles)
    now = monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
        while _requests and now - _requests[0] >= 3600:
            _requests.popleft()
        if len(_requests) >= MAX_REQUESTS_PER_HOUR:
            raise SearchLimitReached("Craigslist research limit reached; try later")
        _requests.append(now)
    url = "https://www.craigslist.org/search/sss?" + urlencode({
        "query": query, "postal": zip_code, "search_distance": radius_miles,
        "sort": "date"})
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=12) as response:
            if response.status != 200 or "text/html" not in response.headers.get("Content-Type", ""):
                raise SearchUnavailable("Craigslist did not return a search page")
            page = response.read(MAX_HTML_BYTES + 1)
            if len(page) > MAX_HTML_BYTES:
                raise SearchUnavailable("Craigslist search page was too large")
            html = page.decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError) as error:
        raise SearchUnavailable("Craigslist search is unavailable") from error
    if "cl-static-search-results" not in html:
        raise SearchUnavailable("Craigslist search results were unavailable")
    result = {"source": "craigslist", "query": query, "search_url": url,
              "searched_at": datetime.now(timezone.utc).isoformat(),
              "price_basis": "unreviewed_active_asking_prices",
              "candidates": [row for row in parse_search_html(html, limit=30)
                             if _matches_query(row["title"], query)][:12]}
    with _lock:
        _cache[key] = (monotonic(), result)
    return result
