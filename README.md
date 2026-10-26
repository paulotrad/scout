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
$env:SCOUT_CF_TEAM_DOMAIN = 'sparkling-frost-1ea7.cloudflareaccess.com'
$env:SCOUT_CF_AUD = '9d46018cd7f9357ee641888bcc3b6a8e3cd6b1a11bed64821be951df9fa268ae'
python marketplace_web.py --port 8765
```

The tunnel forwards `/Facebooknotify` and its subpaths to `http://127.0.0.1:8765`. The Cloudflare Access self-hosted application `Scout Facebook Marketplace` protects that path and uses the `Scout-Google-Auth` identity provider. Scout validates the Access application JWT signature, issuer, audience, and expiration on every public request. The verified email selects that user's profile, scans, and alerts; browser-supplied email values are ignored. The local `127.0.0.1` site remains a development view. Billing is a preview, not a connected service.

## Match emails through Gmail

Scout groups the keyword scans in one watchlist run into a single email to that profile's verified address. It creates an email only when at least one **new** physical item passes the saved keyword, price, radius, and condition checks. A run with no new matches sends nothing. Failed keyword scans do not prevent a summary of matches from the other scans. Email is stored in a retryable SQLite outbox before delivery.

Each profile has an **Email me new matches** checkbox in its plan settings. Existing profiles start with it on. Turning it off removes unsent summaries and prevents new ones; in-app match alerts continue. Turning it back on applies to future scan runs.

The sender is `fca.21050@gmail.com`. To activate it on this Windows computer:

1. Turn on [Google 2-Step Verification](https://myaccount.google.com/security) for `fca.21050@gmail.com` if needed, then create a dedicated [Google app password](https://myaccount.google.com/apppasswords) named for Scout. Google sign-in through Cloudflare does not grant Gmail sending access.
2. In PowerShell, from this folder, run `./start_scout.ps1 -SetGmailPassword`. The script prompts for the app password without displaying it and stores it encrypted for this Windows user in the ignored `runs/gmail_smtp_password.xml` file. Do not use your regular Google password or put the app password in Git, chat, or a `.env` file.
3. The script checks the Gmail SMTP login **without sending an email**, then restarts Scout if no scan is active. Afterward, the Admin page shows `SMTP ready`. On later restarts, run `./start_scout.ps1` to reuse the saved encrypted password. Run with `-SetGmailPassword` again if you rotate it.

Gmail SMTP uses `smtp.gmail.com:587` with STARTTLS and the full Gmail address as username. The password is required only on the Scout computer. Until it is configured, matching summaries remain pending in SQLite and are sent after a sender is connected. Scout does not send a test email or a no-match email.

## Admin roles

On the Scout computer, grant or remove admin access using the verified Google email address:

```powershell
python marketplace_admin.py add person@example.com
python marketplace_admin.py list
python marketplace_admin.py remove person@example.com
```

Admin roles are stored in `runs/marketplace.sqlite`. On the public site, only an admin's verified Cloudflare Access email can open `/Facebooknotify/admin` and `/Facebooknotify/api/admin`. The Admin link appears after login for those accounts. Admins can review all saved profiles and scans. The local `127.0.0.1` admin view remains available for development. Back up the SQLite database to preserve roles and profiles.

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
- `marketplace_settings.example.json` supplies starter scan settings. Scout saves live local settings to the ignored `marketplace_settings.json` file.

Run the tests with `python -m unittest discover -s tests`. To exercise card parsing without opening Facebook, use `python fb_marketplace.py --fixture data/facebook_cards_demo.json --output runs/facebook_demo.json`.
