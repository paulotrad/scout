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

On the current Scout computer, the Windows task **Scout Marketplace Health** checks the local server at sign-in and every five minutes. It starts Scout if the process is gone and restarts a process that stays unresponsive across checks. A healthy server and an active scan are left alone. `start_scout.ps1 -EnsureRunning` is the task's health command; ordinary `start_scout.ps1` still checks Gmail before a manual restart. Automatic startup does not wait for Gmail to respond, and unsent match emails stay in the SQLite outbox for retry. The computer must be awake and the Windows account signed in for scheduled scans to run. Cloudflare's tunnel is a separate automatic Windows service.

Scheduled keyword jobs are saved in SQLite before they run. A missed fast check can start within 20 minutes of its half-hour slot, and a missed 7 a.m. or noon scan can start within two hours. Failed scheduled jobs get one delayed retry. Scout recognizes an already-started job after a restart so the same slot is not scanned twice.

The **Scan health** page at `/notify/health` shows each saved search's latest attempt, latest completed check, next scheduled window, checked-card count, unfinished analyses, and errors. Its 48-hour totals use saved scans and in-app alerts. A 14-day Scout benchmark is stored locally in `runs/scout_benchmark.json`; the page shows progress and offers a CSV of measured checks. A changed keyword, ZIP, radius, or price range starts a new cohort rather than mixing old settings. The benchmark currently measures Scout only: it cannot claim a missed-listing rate, seller-post-to-alert delay, or competitor comparison without independently collected observations. The alert time metric starts at the beginning of Scout's scan. Feedback votes are optional and are not an overall accuracy rate.

For the Cloudflare route at `https://app.thedeveloperpaul.com/notify/`, start the server with:

```powershell
$env:SCOUT_PUBLIC_HOST = 'app.thedeveloperpaul.com'
$env:SCOUT_PUBLIC_WRITES = '1'
$env:SCOUT_CF_TEAM_DOMAIN = 'sparkling-frost-1ea7.cloudflareaccess.com'
$env:SCOUT_CF_AUD = '9d46018cd7f9357ee641888bcc3b6a8e3cd6b1a11bed64821be951df9fa268ae'
python marketplace_web.py --port 8765
```

The tunnel forwards `/notify` and its subpaths to `http://127.0.0.1:8765`. The earlier `/Facebooknotify` route redirects to `/notify`. The Cloudflare Access self-hosted application `Scout Facebook Marketplace` protects both paths and uses the `Scout-Google-Auth` identity provider. Scout validates the Access application JWT signature, issuer, audience, and expiration on every public request. The verified email selects that user's profile, scans, and alerts; browser-supplied email values are ignored. The local `127.0.0.1` site remains a development view. Billing is a preview, not a connected service.

## Match emails through Gmail

Scout groups the keyword scans in one watchlist run into a single email to that profile's verified address. It creates an email only when at least one **new** listing passes the saved keyword, price, radius, and applicable condition checks. Pet rehoming posts can match without a condition grade. A run with no new matches sends nothing. Failed keyword scans do not prevent a summary of matches from the other scans. Email is stored in a retryable SQLite outbox before delivery.

Each match in the email and on the Alerts page shows the saved photo when available, the effective seller asking price, approximate distance, a short match reason, and a recent asking range when enough compatible comparisons exist. Sparse mower variants can use broader-area references, and Scout labels that scope and lowers confidence. A Facebook card marked FREE with a different seller ask in the description is called out. Links open either the original Facebook listing or Scout's saved analysis. Email photos are embedded from SQLite; unavailable or oversized images are omitted rather than linked to a private page.

The Alerts section opens before plan settings. Each profile can save promising listings, hide irrelevant ones, and restore either group to the Inbox. These choices are stored per alert in SQLite and are visible in separate paged Inbox, Saved, and Hidden views. Opening Scout analysis marks an alert read; saving or hiding it also clears its unread badge. The original scan and Facebook links remain available in every view.

New match emails include **👍 Yes** and **👎 Needs correction** links for each item. The links open that item's Scout card; opening a link does not cast a vote. The signed-in person confirms the vote in Scout, chooses whether the item, condition, or price is wrong, and can enter the correct value. Votes and proposed corrections are saved per account and Marketplace listing in SQLite, so they remain visible if that listing appears in another scan. Admin shows corrections alongside recent uncertain items. They are review data; they do not automatically retrain or override the model.

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

Admin roles are stored in `runs/marketplace.sqlite`. On the public site, only an admin's verified Cloudflare Access email can open `/notify/admin` and `/notify/api/admin`. The Admin link appears after login for those accounts. Admins can review all saved profiles and scans. The local `127.0.0.1` admin view remains available for development. Back up the SQLite database to preserve roles and profiles.

## What a scan does

Each saved search has its own keyword, ZIP code, radius, listed-price range, and minimum condition. The free plan schedules a daily 7 a.m. scan in the profile's selected timezone; the premium preview also schedules noon. A manual scan can be started from the site. Searches are queued and run one at a time.

The pipeline collects Marketplace cards, reads seller descriptions, applies the radius and price limits, identifies items with `qwen3-vl:8b-instruct`, summarizes descriptions with `qwen3:8b`, and uses the vision model again for condition. Service ads, discounts, businesses, and posts without an identifiable item are filtered from item results. Pet rehoming posts remain visible and alertable; Scout does not assign them a condition grade or price comparison. Results and stage progress appear on each scan page. Price estimates based on asking-price comparisons are provisional, not sold-price data.

Clear rental, for-hire, and seller-advertised service offers are excluded from item alerts, even when their photos show a physical product. If a scan finds no cards inside the requested radius, its page shows the closest visible cards without obvious service wording outside the radius and any cards whose seller town could not be checked. Those outside cards were not fully classified; they are for context only, never treated as matches or emailed.

Scheduled checks collect up to 120 visible cards over eight scrolls, skip unchanged listings already reviewed for that saved search, and analyze up to 40 new or edited cards. Extra cards remain eligible for the next check. Scout stores each reviewed card's title and price per saved search, so a visible change receives another analysis; legacy cards without that fingerprint receive one refresh. The existing per-person alert record prevents duplicate emails for the same listing. The scan page shows how many cards were checked, unchanged, rechecked, analyzed, excluded, or deferred. A temporary model connection failure or incomplete JSON gets one retry. When every eligible item lacks a usable photo analysis, the scan fails visibly and can be retried instead of reporting an empty successful scan. Email summaries say whether a match came from the seller title, seller description, Scout's item identification, or a free card, even when no price comparison is ready.

Scout keeps Qwen3-VL 8B as the default vision model. For each scan it can send at most two listings with an uncertain item category, conflicting identity clues, a condition-model error, or unresolved visible damage to the locally installed `gemma3:12b` for an independent second opinion. The larger model does not silently overwrite the original identification or condition; disagreements appear on the item card and pause its price rating. Ordinary unknown condition caused by missing seller evidence does not trigger another model call. Reviews are cached by listing photos and seller text. Set `SCOUT_SECOND_OPINION_MODEL=off` before starting Scout to disable this stage, or set it to another installed local vision model.

The mower discovery-depth pilot is saved in `runs/fast_coverage_pilot.json` and is currently stopped. It measured card discovery in repeated local searches, not price coverage. Reviewed mower asking prices now expand from 30 to 60, 120, 250, and 500 miles until at least three compatible references are available. Battery, gas, corded, deck size, voltage, included battery and charger, and condition still constrain matches. Named gas mower brands are compared with the same brand. Electric scooters require a confirmed model before a price rating; generic scooter prices can mix children's and adult products. Other product types remain local. A wider search of saved references cannot supply a missing variant or a missing seller detail; those listings remain unrated until suitable references are collected.

The admin **Price research** page shows items with fewer than three compatible asking prices. A separate queue checks another metro area for mower listings whose brand, power source, deck size, and battery kit are clear, and for electric scooters with an explicit Razor model number. Clear, single-piece furniture with seller details and a supported condition can instead receive one local search within 60 miles for the same form and named material. It screens seller descriptions, excludes the target listing and duplicate IDs, saves only compatible asking prices, and updates the saved estimate. Mower and scooter categories are capped at six distinct areas; furniture gets one local area. All research shares a twelve-area daily limit. Ambiguous items and furniture sets remain visible as **Needs item review**; they do not become single-item price references. These research checks do not send match emails or change a user's saved search plan.

When a used asking-price comparison is unavailable, an item card offers used-listing search links. Craigslist carries the scan ZIP and radius; OfferUp requires the visitor to confirm their area; smaller shippable goods can also use Mercari. The card can request one nearby Craigslist search page on demand and show up to 12 possible title matches with asking prices and links. This text-only lookup never requests outside listing images or detail pages. The leads are unreviewed and do not change Scout's price rating. A comparable must match the product variant, included parts, condition, and location before its asking price can enter the price bar. The existing Decodo proxy login does not grant access to Decodo's structured Web Data API.

Pricing also pauses complete-item comparisons when seller text clearly says an essential component is missing or the listing is for parts. The item card names the missing parts. Parts-only listings are excluded from the complete-item reference pool; a separate parts cohort needs its own evidence before Scout can rate those listings.

Recognized furniture and appliance sets are itemized before pricing. For example, two chairs and one couch use chair and couch asking-price references separately; Scout multiplies each unit range by its quantity and adds the subtotals. Whole-bundle listings are excluded as single-item references. Unclear piece counts, missing component references, or a seller price that might be per item pause the bundle price rating. The sum is an asking-price guide, not a completed bundle sale price.

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
