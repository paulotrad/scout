"""Craigslist research stays text-only and separate from rated comparables."""

import unittest
from unittest.mock import patch

import marketplace_craigslist as research


HTML = """<ol class="cl-static-search-results">
<li class="cl-static-search-result"><a href="https://baltimore.craigslist.org/grd/d/ryobi-18v-mower/123.html">
<div class="title">Ryobi 18V 13 inch mower</div><div class="price">$75</div>
<div class="location">Edgewood, MD</div><img src="https://images.craigslist.org/not-fetched.jpg"></a></li>
<li class="cl-static-search-result"><a href="https://baltimore.craigslist.org/cto/d/truck/456.html">
<div class="title">Pickup truck</div><div class="price">$8000</div>
<div class="location">Baltimore, MD</div></a></li>
</ol>"""


class FakeResponse:
    status = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return HTML.encode("utf-8")


class CraigslistResearchTests(unittest.TestCase):
    def test_parses_asking_prices_but_never_fetches_image_or_details(self):
        with (patch.object(research, "_cache", {}),
              patch.object(research, "_requests", research.deque()),
              patch.object(research, "urlopen", return_value=FakeResponse()) as fetch):
            result = research.search_craigslist("ryobi mower", "21009", 45)
        self.assertEqual(fetch.call_count, 1)
        requested = fetch.call_args.args[0].full_url
        self.assertIn("/search/sss?", requested)
        self.assertIn("postal=21009", requested)
        self.assertIn("search_distance=45", requested)
        self.assertNotIn("images.craigslist", requested)
        self.assertEqual(result["price_basis"], "unreviewed_active_asking_prices")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["asking_price"], 75)

    def test_rejects_non_craigslist_links_and_nonpositive_prices(self):
        source = HTML.replace("https://baltimore.craigslist.org/grd/",
                              "https://example.com/grd/")
        self.assertEqual(research.parse_search_html(source), [
            {"title": "Pickup truck", "asking_price": 8000.0,
             "location": "Baltimore, MD",
             "url": "https://baltimore.craigslist.org/cto/d/truck/456.html"}])
        self.assertEqual(research.parse_search_html(HTML.replace("$75", "$0"))[0]
                         ["title"], "Pickup truck")

    def test_requires_valid_area(self):
        with self.assertRaises(ValueError):
            research.search_craigslist("mower", "bad zip", 45)


if __name__ == "__main__":
    unittest.main()
