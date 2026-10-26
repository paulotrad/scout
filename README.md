# Facebook Marketplace Scout

A local web app that scans Facebook Marketplace for saved keywords, keeps listings and photos, and uses local AI to identify physical items and estimate condition. Scans, account profiles, alerts, and images are stored on this computer.

## Run

```powershell
python -m pip install -r requirements.txt
ollama pull qwen3-vl:8b-instruct
ollama pull qwen3:8b
python marketplace_web.py --port 8765
```

Open [the local site](http://127.0.0.1:8765/). Chrome and a matching ChromeDriver are required for live scans. Selenium Manager can obtain the driver on first use. Facebook browsing uses the computer's direct connection, with no proxy. Leave the server running for queued and scheduled scans. The scheduler is a backend thread in the web server, so only one server instance should run against the same database.

For the existing Cloudflare route at `https://app.thedeveloperpaul.com/Facebooknotify/`, start the server with:

```powershell
$env:SCOUT_PUBLIC_HOST = 'app.thedeveloperpaul.com'
$env:SCOUT_PUBLIC_WRITES = '1'
python marketplace_web.py --port 8765
```

The tunnel must forward `/Facebooknotify` and its subpaths to `http://127.0.0.1:8765`. The site is currently open for reading and writing. An email identifies a saved demo profile; it does **not** verify ownership or authenticate the visitor. Billing and email delivery are previews, not connected services.

## What a scan does

Each saved search has its own keyword, ZIP code, radius, listed-price range, and minimum condition. The free plan schedules a daily 7 a.m. scan in the profile's selected timezone; the premium preview also schedules noon. A manual scan can be started from the site. Searches are queued and run one at a time.

The pipeline collects Marketplace cards, reads seller descriptions, applies the radius and price limits, identifies items with `qwen3-vl:8b-instruct`, summarizes descriptions with `qwen3:8b`, and uses the vision model again for condition. Service ads, discounts, and businesses are filtered from item results. Results and stage progress appear on each scan page. Price estimates based on asking-price comparisons are provisional, not sold-price data.

## Files and data

- `marketplace_web.py` serves the site and scheduler; `marketplace_site/` contains the frontend.
- `marketplace_scan_runner.py` runs the scan stages. `fb_marketplace.py` and `marketplace_details.py` use the direct Selenium browser helper in `marketplace_browser.py`.
- `marketplace_*.py` and `product_candidate.py` implement storage, filtering, AI review, pricing, and alerts.
- `runs/marketplace.sqlite` contains profiles, alerts, listings, and image bytes. Back it up to preserve user data.
- `runs/scans/` contains saved scan manifests, intermediate results, and local listing photos. The legacy October 22 scan also needs `runs/facebook_free_21009_within15_conditioned.json` and `runs/facebook_free_21009_candidate_pool_images/`.
- `runs/town_geocodes.json` caches approximate distances. Seller towns can make radius checks approximate.
- `data/` has sample cards and an example product catalog for local testing.

Run the tests with `python -m unittest discover -s tests`. To exercise card parsing without opening Facebook, use `python fb_marketplace.py --fixture data/facebook_cards_demo.json --output runs/facebook_demo.json`.
