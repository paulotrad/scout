const app = document.getElementById("app");
const picker = document.getElementById("scan-picker");
const basePath = location.pathname === "/Facebooknotify" ||
  location.pathname.startsWith("/Facebooknotify/") ? "/Facebooknotify" : "";
const appPath = path => `${basePath}${path}`;
const pagePath = location.pathname.slice(basePath.length) || "/";
const scanPath = /^\/scan\/(scan-[a-z0-9-]+)$/.exec(pagePath);
const publicHost = Boolean(basePath) && !["127.0.0.1", "localhost"].includes(location.hostname);
const publicReadOnly = Boolean(basePath) && document.body.dataset.publicWrites !== "true";
let pollTimer = null;
let activeAccount = null;
const selectedEmail = () => publicHost ? (activeAccount?.email || "") :
  (localStorage.getItem("scout_demo_email") || "");

if (publicReadOnly) {
  for (const selector of [".header-alert-link", ".header-link"]) {
    document.querySelectorAll(selector).forEach(element => { element.style.display = "none"; });
  }
}
if (publicHost) document.querySelector(".admin-link").style.display = "none";
const showAdminLink = account => {
  if (publicHost) document.querySelector(".admin-link").style.display =
    account?.role === "admin" ? "" : "none";
};

const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[char]));
const cleanLabel = value => String(value || "unknown").replaceAll("_", " ");
const prettyDate = value => value
  ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value))
  : "Date unavailable";
const prettyDateInZone = (value, zone) => {
  if (!value) return "Date unavailable";
  try {
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short",
      timeZone: zone }).format(new Date(value));
  } catch { return prettyDate(value); }
};
const money = value => typeof value === "number" && Number.isFinite(value)
  ? (value === 0 ? "FREE" : new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(value))
  : "Price unknown";
const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
const scanCountLabel = scan => {
  if (scan.status === "complete") {
    if (!Number.isInteger(scan.result_count)) return "Results unavailable";
    const count = scan.result_count;
    return `${count} ${count === 1 ? "item" : "items"} shown`;
  }
  if (scan.status === "running" && scan.stage_progress) {
    const {label, processed, total} = scan.stage_progress;
    return `${label}: ${processed}/${total}`;
  }
  return ({running: "Scan running", failed: "Scan failed",
    interrupted: "Scan interrupted"})[scan.status] || "Waiting to scan";
};

function showAlertCount(count) {
  document.getElementById("alert-count").textContent = String(count);
}

async function api(path, options = {}) {
  const response = await fetch(appPath(path), { cache: "no-store", ...options });
  let result;
  try { result = await response.json(); } catch { result = {}; }
  if (!response.ok) {
    const message = response.status === 403 && !result.error
      ? "This preview returned 403 before Scout received the action. Open Scout in Edge on this computer to save or start a scan."
      : (result.error || `Request failed (${response.status})`);
    throw new Error(message);
  }
  return result;
}

const accountApi = (path, options = {}) => api(path, {
  ...options,
  headers: {...options.headers,
    ...(!publicHost && selectedEmail() ? {"X-Scout-Email": selectedEmail()} : {})}
});

function scanName(scan) {
  const terms = (scan.keywords || []).join(", ");
  return `${terms ? `${terms} · ` : ""}${scan.zip_code} · ${scan.radius_miles} mi · ${prettyDate(scan.created_at)}`;
}

function fillPicker(scans, selected = "") {
  picker.replaceChildren(new Option("Choose a scan", ""));
  for (const scan of scans) picker.add(new Option(scanName(scan), scan.id));
  picker.value = selected;
}
picker.addEventListener("change", () => {
  if (picker.value) location.href = appPath(`/scan/${encodeURIComponent(picker.value)}`);
});

function statusTag(status) {
  const name = ["complete", "running", "queued", "failed", "interrupted"].includes(status)
    ? status : "queued";
  return `<span class="status ${name}">${esc(name)}</span>`;
}

function historyRow(scan) {
  return `<article class="panel history-row">
    <div>
      <h3>${esc(scan.zip_code)} <span aria-hidden="true">·</span> ${esc(scan.radius_miles)} mile radius</h3>
      <div class="meta-line">
        <span>${esc(prettyDate(scan.created_at))}</span>
        <span>${esc((scan.keywords || []).join(", "))}</span>
        ${scan.watch_item ? `<span>Listed $${esc(scan.watch_item.min_price)}–$${esc(scan.watch_item.max_price)}</span>` : ""}
        ${statusTag(scan.status)}
      </div>
    </div>
    <div class="scan-count">${esc(scanCountLabel(scan))}</div>
    ${scan.id
      ? `<a class="open-link" href="${appPath(`/scan/${encodeURIComponent(scan.id)}`)}">Open results →</a>`
      : '<span class="queue-waiting">Waiting its turn</span>'}
  </article>`;
}

function queueContent(queue) {
  return `<div class="section-heading"><div><p class="eyebrow">In progress</p>
      <h2 id="queue-title">Scan queue</h2></div>
      <p class="queue-count">${esc(plural(queue.length, "keyword"))}</p></div>
    <div class="history-list queue-list">${queue.map(historyRow).join("")}</div>`;
}

function queueSection(queue) {
  return `<section class="history" id="scan-queue" aria-labelledby="queue-title"
    ${queue.length ? "" : "hidden"}>${queueContent(queue)}</section>`;
}

function updateQueueSection(queue) {
  const section = document.getElementById("scan-queue");
  if (!section) return;
  section.hidden = !queue.length;
  section.innerHTML = queueContent(queue);
}

function queueForDisplay(history, account) {
  if (Array.isArray(history.queue)) return history.queue;
  // Older running servers have not loaded the queue API yet. The watchlist
  // batch always starts in keyword order, so show the remaining saved terms.
  const active = (history.scans || []).find(scan =>
    scan.id === history.active_scan_id && scan.status === "running" &&
    scan.watch_item && scan.mode === "scheduled");
  if (!active || !account) return [];
  const items = account.watch_items || [];
  const position = items.findIndex(item =>
    item.term.toLowerCase() === active.watch_item.term.toLowerCase());
  if (position < 0) return [active];
  return [active, ...items.slice(position + 1).map(item => ({
    id: null, status: "queued", created_at: active.created_at,
    zip_code: item.zip_code, radius_miles: item.radius_miles,
    keywords: [item.term], watch_item: item
  }))];
}

function watchRows(items, locks, globals) {
  return items.map((item, index) => `<div class="watch-item-row" data-watch-term="${esc(item.term)}">
    <div class="watch-item-name"><strong>${esc(item.term)}</strong><small>Scan ${index + 1}</small></div>
    <div class="field"><label>ZIP code<input data-watch-field="zip_code" inputmode="numeric" pattern="[0-9]{5}" maxlength="5" required
      value="${esc(locks.zip_code ? globals.zip_code : item.zip_code)}" ${locks.zip_code ? "disabled" : ""}></label></div>
    <div class="field"><label>Radius (mi)<input data-watch-field="radius_miles" type="number" min="1" max="100" step="1" required
      value="${esc(locks.radius_miles ? globals.radius_miles : item.radius_miles)}" ${locks.radius_miles ? "disabled" : ""}></label></div>
    <div class="field"><label>Min ($)<input data-watch-field="min_price" type="number" min="0" max="1000000" step="1" required
      value="${esc(locks.min_price ? globals.min_price : item.min_price)}" ${locks.min_price ? "disabled" : ""}></label></div>
    <div class="field"><label>Max ($)<input data-watch-field="max_price" type="number" min="0" max="1000000" step="1" required
      value="${esc(locks.max_price ? globals.max_price : item.max_price)}" ${locks.max_price ? "disabled" : ""}></label></div>
  </div>`).join("");
}

function quickCardRow(card) {
  const description = card.description || "Seller description unavailable.";
  const brief = description.length > 360 ? description.slice(0, 357) + "…" : description;
  const link = sourceUrl(card.url);
  return `<li class="quick-row"><strong>${esc(card.title || "Untitled listing")}</strong>
    <p>${esc(brief)}</p>${card.reason ? `<small>${esc(card.reason)}</small>` : ""}
    ${link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">Open Facebook listing ↗</a>` : ""}</li>`;
}

function adminScanRow(scan) {
  const relation = scan.relationship === "account" ? "Account scan" :
    scan.relationship === "area_match" ? "Same area · owner unverified" : "No saved account or area match";
  return `<article class="admin-scan-row">
    <div><strong>${esc(prettyDate(scan.created_at))}</strong>
      <div class="meta-line"><span>${esc((scan.keywords || []).join(", ") || "No keywords")}</span>
        <span>${esc(scan.zip_code)} · ${esc(scan.radius_miles)} mi</span>
        <span>${esc(relation)}</span>${statusTag(scan.status)}</div></div>
    <span class="scan-count">${esc(scanCountLabel(scan))}</span>
    <a class="open-link" href="${appPath(`/scan/${encodeURIComponent(scan.id)}`)}">Open scan →</a>
  </article>`;
}

function adminAccount(profile) {
  const plan = profile.plan === "plus_preview" ? "Plus preview" : "Free";
  const slots = profile.daily_scan_times.map(value => value === "07:00" ? "7:00 a.m." : "noon").join(" and ");
  const areas = new Map();
  for (const scan of profile.scans) {
    const key = `${scan.zip_code}:${scan.radius_miles}`;
    if (!areas.has(key)) areas.set(key, []);
    areas.get(key).push(scan);
  }
  return `<details class="panel admin-account">
    <summary><span><strong>${esc(profile.display_name)}</strong>
      <small>${esc(profile.email || "Email not attached yet")} · ${esc(profile.zip_code)} · ${esc(profile.radius_miles)} mile radius · ${esc(plan)}</small></span>
      <span class="admin-summary-right">${esc(plural(profile.scan_count, "scan"))}<span aria-hidden="true">⌄</span></span></summary>
    <div class="admin-account-body">
      <div class="admin-profile-grid">
        <div><span class="stat-label">Account email</span><strong>${esc(profile.email || "Not attached")}</strong></div>
        <div><span class="stat-label">Plan status</span><strong>${esc(profile.subscription_status === "preview" ? "Preview · no payment" : "Free · no payment")}</strong></div>
        <div><span class="stat-label">Daily scan times</span><strong>${esc(slots)}</strong><small>${esc(profile.timezone)}</small></div>
        <div><span class="stat-label">Next scheduled scan</span><strong>${esc(prettyDateInZone(profile.next_run_at, profile.timezone))}</strong></div>
        <div><span class="stat-label">Search terms</span><strong>${esc((profile.terms || []).join(", "))}</strong></div>
        <div><span class="stat-label">Email notifications</span><strong>${profile.email_notifications_enabled === false ? "Off" : "On"}</strong></div>
      </div>
      <h3>Scans by area</h3>
      <p class="form-help">Older scans with the same ZIP and radius are shown as area matches when account ownership was not recorded.</p>
      ${profile.scans.length ? Array.from(areas.values()).map(scans =>
        `<div class="admin-area"><h4>${esc(scans[0].zip_code)} · ${esc(scans[0].radius_miles)} mile radius</h4>
        <div class="admin-scan-list">${scans.map(adminScanRow).join("")}</div></div>`).join("")
        : '<p class="admin-empty">No scans for this area yet.</p>'}
    </div>
  </details>`;
}

async function showAdmin() {
  try {
    const [history, matches, account] = await Promise.all([
      api("/api/scans"), api("/api/alerts"), publicHost ? api("/api/account") : Promise.resolve(null)
    ]);
    showAdminLink(account);
    let overview;
    try {
      overview = await api("/api/admin");
    } catch (failure) {
      if (publicHost || !["Request failed (404)", "Page not found"].includes(failure.message)) throw failure;
      // The already-running local server can serve this page before its next restart.
      const account = await api("/api/account");
      const scans = history.scans || [];
      const areaScans = scans.filter(scan => account.configured &&
        scan.zip_code === account.zip_code && scan.radius_miles === account.radius_miles);
      const areaIds = new Set(areaScans.map(scan => scan.id));
      overview = {
        accounts: account.configured ? [{...account, id: "demo",
          subscription_status: account.plan === "plus_preview" ? "preview" : "free",
          daily_scan_times: account.plan === "plus_preview" ? ["07:00", "12:00"] : ["07:00"],
          scans: areaScans.map(scan => ({...scan, relationship: "area_match"})),
          scan_count: areaScans.length}] : [],
        profile_count: account.configured ? 1 : 0,
        paid_subscriber_count: 0,
        preview_count: account.configured && account.plan === "plus_preview" ? 1 : 0,
        total_scan_count: scans.length,
        unassigned_scans: scans.filter(scan => !areaIds.has(scan.id))
      };
    }
    fillPicker(history.scans);
    showAlertCount(matches.unread_count);
    app.innerHTML = `<section class="admin-hero">
      <p class="eyebrow">${publicHost ? "Admin · verified Google login" : "Local admin · open preview"}</p><h1>Accounts and scans</h1>
      <p class="lead">See each saved profile, its search area, schedule, and scans. Paid billing is not connected yet.</p>
    </section>
    <div class="admin-stats">
      <div class="panel stat"><span class="stat-label">Saved profiles</span><strong class="stat-value">${esc(overview.profile_count)}</strong></div>
      <div class="panel stat"><span class="stat-label">Paid subscribers</span><strong class="stat-value">${esc(overview.paid_subscriber_count)}</strong></div>
      <div class="panel stat"><span class="stat-label">Plus previews</span><strong class="stat-value">${esc(overview.preview_count)}</strong></div>
      <div class="panel stat"><span class="stat-label">Saved scans</span><strong class="stat-value">${esc(overview.total_scan_count)}</strong></div>
    </div>
    ${overview.admin_emails?.length ? `<section class="admin-section" aria-labelledby="admin-roles-title">
      <div class="section-heading"><div><p class="eyebrow">Access</p><h2 id="admin-roles-title">Admins</h2></div></div>
      <div class="panel"><p>${overview.admin_emails.map(esc).join(" · ")}</p></div>
    </section>` : ""}
    ${overview.email_delivery ? `<section class="admin-section" aria-labelledby="admin-email-title">
      <div class="section-heading"><div><p class="eyebrow">Notifications</p><h2 id="admin-email-title">Match emails</h2></div></div>
      <div class="panel"><p>${overview.email_delivery.configured ? "SMTP ready" : "Gmail SMTP setup needed"}
        · ${esc(overview.email_delivery.pending)} pending · ${esc(overview.email_delivery.sent)} sent</p>
        <p class="form-help">Scout sends one email after a scan batch only when new items meet the saved criteria.</p></div>
    </section>` : ""}
    <section class="admin-section" aria-labelledby="admin-accounts-title">
      <div class="section-heading"><div><p class="eyebrow">Search areas</p><h2 id="admin-accounts-title">Profiles</h2></div>
        <p>Expand a profile to review its scans.</p></div>
      <div class="admin-accounts">${overview.accounts.length
        ? overview.accounts.map(adminAccount).join("")
        : '<div class="panel empty">No saved profiles yet. Set up a plan to create the first one.</div>'}</div>
    </section>
    ${overview.unassigned_scans.length ? `<section class="admin-section" aria-labelledby="unassigned-title">
      <div class="section-heading"><div><p class="eyebrow">Other areas</p><h2 id="unassigned-title">Unassigned scans</h2></div>
        <p>These do not match a saved profile’s current area.</p></div>
      <div class="admin-scan-list panel">${overview.unassigned_scans.map(scan => adminScanRow({...scan, relationship: "unassigned"})).join("")}</div>
    </section>` : ""}`;
  } catch (failure) {
    app.innerHTML = `<div class="panel empty">Could not load admin overview: ${esc(failure.message)}</div>`;
  }
}

async function showPublicHome() {
  try {
    const history = await api("/api/scans");
    fillPicker(history.scans);
    app.innerHTML = `<section class="hero"><p class="eyebrow">Scout results</p>
      <h1>Marketplace scans</h1>
      <p class="lead">Browse saved scans and their AI item results.</p></section>
      <section class="history-section" aria-labelledby="history-title">
        <div class="section-heading"><div><p class="eyebrow">Archive</p>
          <h2 id="history-title">Saved scans</h2></div>
          <span>${esc(plural(history.scans.length, "scan"))}</span></div>
        <div class="history-list">${history.scans.length
          ? history.scans.map(historyRow).join("")
          : '<div class="panel empty">No scans have been saved yet.</div>'}</div>
      </section>`;
  } catch (failure) {
    app.innerHTML = `<div class="panel empty">Could not load scans: ${esc(failure.message)}</div>`;
  }
}

const timezones = [
  ["America/New_York", "Eastern"], ["America/Chicago", "Central"],
  ["America/Denver", "Mountain"], ["America/Phoenix", "Arizona"],
  ["America/Los_Angeles", "Pacific"], ["America/Anchorage", "Alaska"],
  ["Pacific/Honolulu", "Hawaii"], ["America/Puerto_Rico", "Puerto Rico"]
];

function alertRow(alert) {
  const target = appPath(`/scan/${encodeURIComponent(alert.scan_id)}#item-${encodeURIComponent(alert.listing_id)}`);
  return `<a class="panel alert-row ${alert.read_at ? "" : "unread"}" href="${esc(target)}" data-read-alert="${esc(alert.id)}">
    <div><h3>${esc(alert.title)}</h3>
      <p>${esc(alert.item_type || "Item")} · ${esc(cleanLabel(alert.condition_grade))} condition · ${esc(alert.zip_code)} · ${esc(prettyDate(alert.matched_at))}</p></div>
    <span class="open-link">View item →</span>
  </a>`;
}

async function showHome() {
  try {
    let email = selectedEmail();
    if (!email && !publicHost) {
      fillPicker([]);
      showAlertCount(0);
      app.innerHTML = `<section class="hero"><div>
        <p class="eyebrow">Your Scout profile</p><h1>Open your searches.</h1>
        <p class="lead">Enter your email to load your saved profile, watchlist, scans, and alerts.</p>
        </div><div class="radar" aria-hidden="true"></div></section>
        <section class="panel plan-panel"><form id="open-profile-form">
          <div class="account-email-row"><div class="field"><label for="account-email">Email address</label>
            <input id="account-email" type="email" autocomplete="email" maxlength="254" required placeholder="you@example.com"></div>
            <button class="button" type="submit">Open or create profile</button></div>
          <p class="form-help">Profiles are saved on this Scout computer. Email ownership is not verified in this demo.</p>
          <p class="form-error" id="profile-error" role="alert"></p>
        </form></section>`;
      document.getElementById("open-profile-form").addEventListener("submit", async event => {
        event.preventDefault();
        const button = event.currentTarget.querySelector("button");
        const error = document.getElementById("profile-error");
        button.disabled = true;
        error.textContent = "";
        try {
          const account = await api("/api/account/open", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email: document.getElementById("account-email").value.trim()})
          });
          localStorage.setItem("scout_demo_email", account.email);
          showHome();
        } catch (failure) {
          error.textContent = failure.message;
          button.disabled = false;
        }
      });
      return;
    }
    const [history, account, matches] = await Promise.all([
      accountApi("/api/scans"),
      accountApi("/api/account"), accountApi("/api/alerts")
    ]);
    activeAccount = account;
    showAdminLink(account);
    email = account.email || email;
    const lastManualScan = history.scans.find(scan => scan.mode === "manual");
    const settings = {
      zip_code: lastManualScan?.zip_code || account.zip_code,
      radius_miles: lastManualScan?.radius_miles || account.radius_miles,
      keywords: lastManualScan?.keywords || account.terms
    };
    const emailMode = account.account_schema_version >= 2;
    const locks = account.global_locks || {zip_code: true, radius_miles: true, min_price: true, max_price: true};
    const defaults = {zip_code: account.zip_code, radius_miles: account.radius_miles,
      min_price: account.min_price, max_price: account.max_price};
    let watchDraft = (account.watch_items || account.terms.map(term => ({term, ...defaults})))
      .map(item => ({...item}));
    const nextMessage = account.configured
        ? `Next scan: ${prettyDateInZone(account.next_run_at, account.timezone)} (${account.timezone})`
        : "Save a plan to schedule scans";
    fillPicker(history.scans);
    showAlertCount(matches.unread_count);
    app.innerHTML = `
      <section class="hero">
        <div>
          <p class="eyebrow">${publicHost ? `Signed in as ${esc(account.email)}` : "Instant demo account · no password"}</p>
          <h1>See what’s hiding near you.</h1>
          <p class="lead">Set your watchlist once. Scout searches your area on your plan’s schedule and alerts you when a physical item matches.</p>
        </div>
        <div class="radar" aria-hidden="true"></div>
      </section>
      <section class="plan-section" aria-labelledby="plan-title">
        <div class="section-heading"><div><p class="eyebrow">Your plan</p><h2 id="plan-title">Set up your searches</h2></div>
          <p>${esc(nextMessage)}</p></div>
        <div class="panel plan-panel">
          <form id="plan-form">
            ${emailMode ? `<div class="account-email-row">
              <div class="field"><label for="account-email">Account email</label>
                <input id="account-email" type="email" autocomplete="email" maxlength="254" required
                  value="${esc(account.email || email)}" ${publicHost ? "readonly" : ""} placeholder="you@example.com"></div>
              ${publicHost ? "" : '<button class="button button-secondary" id="account-open" type="button">Open or create profile</button>'}
            </div>
            <p class="form-help">${publicHost ? "Your Cloudflare login selects this profile." : "Email identifies this open demo profile. No message is sent or verified, so anyone who knows the email can open its plan."}</p>` : ""}
            <div class="plan-options" role="group" aria-label="Choose a plan">
              <label class="plan-card"><input type="radio" name="plan" value="free" ${account.plan === "free" ? "checked" : ""}>
                <span><strong>Free</strong><small>$0 / month</small><span>Each keyword scans daily at 7:00 a.m.</span></span></label>
              <label class="plan-card"><input type="radio" name="plan" value="plus_preview" ${account.plan === "plus_preview" ? "checked" : ""}>
                <span><strong>Plus preview</strong><small>Proposed $10 / month</small><span>Each keyword scans at 7:00 a.m. and noon.</span></span></label>
            </div>
            <p class="form-help">Plus is a free demo preview today. No payment or subscription is created.</p>
            <div class="email-notification-setting">
              <label for="plan-email-notifications"><input id="plan-email-notifications" type="checkbox"
                ${account.email_notifications_enabled !== false ? "checked" : ""}>
                <span><strong>Email me new matches</strong><small>Send one summary when a scan finds new items matching my saved criteria.</small></span></label>
              <p class="form-help">Turning this off stops email summaries. Matches still appear in Scout.</p>
            </div>
            <div class="plan-fields">
              <div class="field"><label for="plan-zip">Default ZIP code</label>
                <input id="plan-zip" inputmode="numeric" pattern="[0-9]{5}" maxlength="5" required value="${esc(account.zip_code)}"></div>
              <div class="field"><label for="plan-radius">Default radius in miles</label>
                <input id="plan-radius" type="number" min="1" max="100" step="1" required value="${esc(account.radius_miles)}"></div>
              <div class="field"><label for="plan-timezone">Scan timezone</label>
                <select id="plan-timezone">${timezones.map(([value, label]) =>
                  `<option value="${esc(value)}" ${account.timezone === value ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></div>
            </div>
            <div class="field terms-field"><label for="plan-terms">Items or keywords to search for <span id="term-count">${esc(account.terms.length)}/25</span></label>
              <textarea id="plan-terms" rows="3" maxlength="2000" required placeholder="couch, sofa, desk">${esc(account.terms.join(", "))}</textarea>
              <p class="form-help">Separate up to 25 terms with commas or new lines. Each term gets its own scan and settings below.</p></div>
            <div class="price-fields">
              <div class="field"><label for="price-min">Default minimum price</label>
                <input id="price-min-range" type="range" min="0" max="100000" step="50" value="${esc(Math.min(account.min_price, 100000))}" aria-label="Minimum price slider">
                <input id="price-min" type="number" min="0" max="1000000" step="1" required value="${esc(account.min_price)}" aria-label="Minimum listed price in dollars"></div>
              <div class="field"><label for="price-max">Default maximum price</label>
                <input id="price-max-range" type="range" min="0" max="100000" step="50" value="${esc(Math.min(account.max_price, 100000))}" aria-label="Maximum price slider">
                <input id="price-max" type="number" min="0" max="1000000" step="1" required value="${esc(account.max_price)}" aria-label="Maximum listed price in dollars"></div>
            </div>
            <div class="watch-settings">
              <h3>Settings for each keyword</h3>
              <p class="form-help">Lock a default to use it for every keyword. Unlock it to set a different value for each scan.</p>
              <div class="watch-locks" role="group" aria-label="Apply defaults to every keyword">
                ${[["zip_code", "ZIP code"], ["radius_miles", "Radius"], ["min_price", "Minimum price"], ["max_price", "Maximum price"]]
                  .map(([key, label]) => `<label><input type="checkbox" data-global-lock="${key}" ${locks[key] ? "checked" : ""}> Lock ${label}</label>`).join("")}
              </div>
              <div class="watch-items" id="watch-items">${watchRows(watchDraft, locks, defaults)}</div>
            </div>
            <div class="plan-bottom"><div class="field"><label for="plan-grade">Minimum AI condition</label>
              <select id="plan-grade"><option value="any">Any, including unknown</option>
                <option value="needs_repair">Needs repair or better</option>
                <option value="fair">Fair or better</option><option value="good">Good or better</option>
                <option value="like_new">Like new or new</option><option value="new">New only</option></select></div>
              <button class="button" type="submit">${account.configured ? "Update plan setup" : "Save plan setup"}</button>
              <button class="button button-secondary" id="plan-scan-now" type="button" ${account.configured ? "" : "disabled"}>Scan watchlist now</button></div>
            <p class="form-error" id="plan-error" role="alert"></p>
          </form>
        </div>
        <p class="caution">Scans run only while this computer and Scout are running. Scheduled scans check up to 20 visible cards per term; results are a sampled search, not a complete Marketplace inventory.</p>
      </section>
      <section class="panel setup-panel" aria-labelledby="setup-title">
        <div class="section-heading">
          <div><h2 id="setup-title">Start a scan</h2><p>Free listings are the starting search.</p></div>
        </div>
        <form id="scan-form">
          <div class="form-grid">
            <div class="field"><label for="zip-code">ZIP code</label>
              <input id="zip-code" name="zip" inputmode="numeric" pattern="[0-9]{5}" maxlength="5" autocomplete="postal-code" required value="${esc(settings.zip_code || "21009")}">
            </div>
            <div class="field"><label for="radius">Radius in miles</label>
              <input id="radius" name="radius" type="number" min="1" max="100" step="1" required value="${esc(settings.radius_miles || 15)}">
            </div>
            <div class="field"><label for="keywords">Keywords</label>
              <input id="keywords" name="keywords" maxlength="2000" value="${esc((settings.keywords || ["free"]).join(", "))}" placeholder="free, desk, couch">
            </div>
            <button class="button" type="submit">Start scan <span aria-hidden="true">&nbsp;→</span></button>
          </div>
          <p class="form-help">One-off scan saved to ${esc(email)}. Separate up to 25 keywords with commas. Facebook may apply a wider search radius; Scout filters by seller town.</p>
          <p class="form-error" id="form-error" role="alert"></p>
        </form>
      </section>
      ${queueSection(queueForDisplay(history, account))}
      <section class="alerts-section" aria-labelledby="alerts-title">
        <div class="section-heading"><div><p class="eyebrow">Matches</p><h2 id="alerts-title">Alerts</h2></div>
          <p>${esc(plural(matches.unread_count, "unread alert"))}</p></div>
        <div class="alerts-list">${matches.alerts.length ? matches.alerts.slice(0, 20).map(alertRow).join("")
          : '<div class="panel empty">New matching items will appear here after a scan.</div>'}</div>
      </section>
      <section class="history" aria-labelledby="history-title">
        <div class="section-heading"><div><p class="eyebrow">Archive</p><h2 id="history-title">Saved scans</h2></div>
          <p id="home-history-count">${esc(plural(history.scans.length, "scan"))}</p></div>
        <div class="history-list" id="home-history-list">${history.scans.length
          ? history.scans.map(historyRow).join("")
          : '<div class="panel empty">No scans yet. Start with your ZIP code above.</div>'}</div>
      </section>`;
    document.getElementById("scan-form").addEventListener("submit", async event => {
      event.preventDefault();
      const form = event.currentTarget;
      const button = form.querySelector("button");
      const error = document.getElementById("form-error");
      const keywords = document.getElementById("keywords").value
        .split(",").map(word => word.trim()).filter(Boolean);
      button.disabled = true;
      error.textContent = "";
      try {
        const scan = await api("/api/scans", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            zip_code: document.getElementById("zip-code").value.trim(),
            radius_miles: Number(document.getElementById("radius").value),
            keywords,
            ...(emailMode && selectedEmail() ? {email: selectedEmail()} : {})
          })
        });
        location.href = appPath(`/scan/${encodeURIComponent(scan.id)}`);
      } catch (failure) {
        error.textContent = failure.message;
        button.disabled = false;
      }
    });
    document.getElementById("plan-grade").value = account.minimum_grade;
    if (emailMode && !publicHost) {
      document.getElementById("account-open").addEventListener("click", async event => {
        const input = document.getElementById("account-email");
        if (!input.reportValidity()) return;
        const button = event.currentTarget;
        const error = document.getElementById("plan-error");
        button.disabled = true;
        error.textContent = "";
        try {
          const opened = await api("/api/account/open", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email: input.value.trim()})
          });
          localStorage.setItem("scout_demo_email", opened.email);
          showHome();
        } catch (failure) {
          error.textContent = failure.message;
          button.disabled = false;
        }
      });
    }
    const watchTerms = () => document.getElementById("plan-terms").value
      .split(/[,\n]/).map(term => term.trim()).filter(Boolean);
    const globalValues = () => ({
      zip_code: document.getElementById("plan-zip").value.trim(),
      radius_miles: Number(document.getElementById("plan-radius").value),
      min_price: Number(document.getElementById("price-min").value),
      max_price: Number(document.getElementById("price-max").value)
    });
    const lockValues = () => Object.fromEntries([...document.querySelectorAll("[data-global-lock]")]
      .map(input => [input.dataset.globalLock, input.checked]));
    const readWatchRows = () => [...document.querySelectorAll("[data-watch-term]")].map(row => {
      const current = {term: row.dataset.watchTerm};
      for (const key of ["zip_code", "radius_miles", "min_price", "max_price"]) {
        const input = row.querySelector(`[data-watch-field="${key}"]`);
        current[key] = key === "zip_code" ? input.value.trim() : Number(input.value);
      }
      return current;
    });
    const syncWatchRows = () => {
      const previous = new Map([...watchDraft, ...readWatchRows()].map(item => [item.term.toLowerCase(), item]));
      const global = globalValues();
      const currentLocks = lockValues();
      watchDraft = watchTerms().map(term => ({...global, ...previous.get(term.toLowerCase()), term}));
      document.getElementById("term-count").textContent = `${watchDraft.length}/25`;
      document.getElementById("watch-items").innerHTML = watchRows(watchDraft, currentLocks, global);
    };
    document.getElementById("plan-terms").addEventListener("input", syncWatchRows);
    document.querySelectorAll("[data-global-lock]").forEach(input =>
      input.addEventListener("change", syncWatchRows));
    for (const id of ["plan-zip", "plan-radius", "price-min", "price-max"])
      document.getElementById(id).addEventListener("input", syncWatchRows);
    for (const name of ["min", "max"]) {
      const range = document.getElementById(`price-${name}-range`);
      const number = document.getElementById(`price-${name}`);
      range.addEventListener("input", () => { number.value = range.value; syncWatchRows(); });
      number.addEventListener("input", () => {
        range.value = String(Math.min(100000, Math.max(0, Number(number.value) || 0)));
      });
    }
    document.getElementById("plan-form").addEventListener("submit", async event => {
      event.preventDefault();
      const button = event.currentTarget.querySelector("button[type=submit]");
      const error = document.getElementById("plan-error");
      button.disabled = true;
      error.textContent = "";
      try {
        if (emailMode && !publicHost && document.getElementById("account-email").value.trim().toLowerCase() !== selectedEmail())
          throw new Error("Open the email profile before saving its plan.");
        await api("/api/account", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            zip_code: document.getElementById("plan-zip").value.trim(),
            radius_miles: Number(document.getElementById("plan-radius").value),
            terms: watchTerms(),
            watch_items: readWatchRows(),
            global_locks: lockValues(),
            min_price: Number(document.getElementById("price-min").value),
            max_price: Number(document.getElementById("price-max").value),
            minimum_grade: document.getElementById("plan-grade").value,
            timezone: document.getElementById("plan-timezone").value,
            plan: document.querySelector('input[name="plan"]:checked').value,
            email_notifications_enabled: document.getElementById("plan-email-notifications").checked,
            ...(emailMode ? {email: selectedEmail()} : {})
          })
        });
        showHome();
      } catch (failure) {
        error.textContent = failure.message;
        button.disabled = false;
      }
    });
    document.getElementById("plan-scan-now").addEventListener("click", async event => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const scan = await api("/api/account/scan", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(emailMode ? {email: selectedEmail()} : {})
        });
        location.href = appPath(`/scan/${encodeURIComponent(scan.id)}`);
      } catch (failure) {
        document.getElementById("plan-error").textContent = failure.message;
        button.disabled = false;
      }
    });
    document.querySelectorAll("[data-read-alert]").forEach(link => {
      link.addEventListener("click", async event => {
        event.preventDefault();
        try {
          await api(`/api/alerts/${link.dataset.readAlert}/read`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(emailMode ? {email: selectedEmail()} : {})
          });
        } catch { /* Opening the saved result still works if marking read fails. */ }
        location.href = link.href;
      });
    });
    if (location.hash === "#alerts-title")
      document.getElementById("alerts-title")?.scrollIntoView();
  } catch (failure) {
    app.innerHTML = `<div class="panel empty">Could not load scans: ${esc(failure.message)}</div>`;
  }
}

function sourceUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && ["facebook.com", "www.facebook.com"].includes(url.hostname)
      ? url.href : "";
  } catch { return ""; }
}

const flagLabels = {
  item_identity_conflict: "The title and photo may describe different objects.",
  condition_not_supported_by_seller_text: "The seller did not give enough condition detail for a quality grade.",
  repair_grade_from_seller_text: "Seller reports a repair need.",
  seller_reports_condition_issue: "Seller reports wear or maintenance.",
  seller_reports_plant_needs_care: "Seller reports the plant needs care.",
  seller_item_overrode_model_type: "Seller title and description corrected the photo model's item type.",
  photo_primary_object_may_be_background: "The photo model may have focused on a background object.",
  store_promotion_not_single_item: "This is a storewide promotion, not one offered item.",
  zero_price_card_may_not_be_free: "Seller text may contradict the listed $0 price.",
  service_ad_not_item: "This advertises pickup or another service, not an offered item.",
  not_a_single_product_listing: "This listing does not clearly offer one item.",
  description_missing: "The seller description was unavailable."
};

function itemCard(item) {
  const rawTitle = item.display_title || (item.asking_price === 0
    ? item.title.replace(/^free\b[\s:!.,;\-–—]*/i, "").replace(/[\s!.,;:\-–—]+$/g, "") || item.title
    : item.title);
  const title = rawTitle[0].toUpperCase() + rawTitle.slice(1);
  const grade = item.condition_grade || "unknown";
  const usage = item.usage_state || "unknown";
  const gradeStyle = grade === "needs_repair" ? "red" : grade === "unknown" ? "amber" : "teal";
  const summary = item.generated_description || item.refactored_description || item.description ||
    "No seller details were available.";
  const evidence = item.condition_evidence?.length
    ? `<ul>${item.condition_evidence.map(point => `<li>${esc(point)}</li>`).join("")}</ul>`
    : "<p>No condition evidence was captured.</p>";
  const flags = item.review_flags?.length
    ? `<ul>${item.review_flags.map(flag => `<li>${esc(flagLabels[flag] || cleanLabel(flag))}</li>`).join("")}</ul>`
    : "<p>Review the photo and seller details before relying on this result.</p>";
  const link = sourceUrl(item.url);
  const distance = typeof item.distance_miles_approx === "number"
    ? `≈ ${item.distance_miles_approx.toFixed(1)} mi away`
    : "Distance unavailable";
  const priceLabel = item.price_source === "seller_description"
    ? `Seller asks ${money(item.asking_price)}`
    : item.price_source === "unconfirmed" ? "Price unclear" : money(item.asking_price);
  return `<article class="panel item-card" id="item-${esc(item.id)}">
    <img class="item-image" src="${esc(appPath(item.image_url))}" alt="Saved thumbnail for ${esc(title)}" loading="lazy">
    <div class="item-body">
      <div class="item-kicker"><span>${esc(distance)}</span><span class="item-price">${esc(priceLabel)}</span></div>
      <h3>${esc(title)}</h3>
      <p class="item-summary">${esc(summary.length > 170 ? summary.slice(0, 167) + "…" : summary)}</p>
      <div class="item-meta">
        <span class="chip">${esc(item.item_type || "item unclear")}</span>
        <span class="chip teal">${esc(usage)}</span><span class="chip ${gradeStyle}">${esc(cleanLabel(grade))}</span>
        ${item.review_flags?.includes("zero_price_card_may_not_be_free")
          ? '<span class="chip red">Price conflict</span>' : ""}
      </div>
      <details>
        <summary>See AI result and seller details</summary>
        <div class="detail-content">
          <dl>
            <dt>AI item category</dt><dd>${esc(item.category || "Unknown")}</dd>
            <dt>Image model</dt><dd>${esc(item.vision_model || "Unavailable")}</dd>
            <dt>Condition model</dt><dd>${esc(item.condition_model || "Unavailable")}</dd>
            ${item.price_source !== "facebook_card" ? `<dt>Facebook card price</dt><dd>${esc(money(item.card_asking_price))}</dd>` : ""}
            ${item.price_source === "seller_description" ? `<dt>Seller asking price</dt><dd>${esc(money(item.seller_asking_price))} in description</dd>` : ""}
            ${typeof item.previous_price === "number" ? `<dt>Other price shown on card</dt><dd>${esc(money(item.previous_price))}; the Facebook card shows ${esc(money(item.card_asking_price ?? item.asking_price))}</dd>` : ""}
            <dt>Suggested identity</dt><dd>${esc(item.identity || "No specific model confirmed")}</dd>
            <dt>Condition estimate</dt><dd>${esc(cleanLabel(grade))} · ${esc(item.condition_confidence || "unrated")} confidence</dd>
            <dt>New or used</dt><dd>${esc(cleanLabel(usage))}</dd>
            <dt>Function</dt><dd>${esc(cleanLabel(item.functional_status))}</dd>
            <dt>Generated item description</dt><dd>${esc(item.generated_description || "Unavailable")}</dd>
            <dt>Original seller title</dt><dd>${esc(item.title)}</dd>
            <dt>Seller description</dt><dd>${esc(item.description || "Unavailable")}</dd>
            <dt>Cleaned description</dt><dd>${esc(item.refactored_description || "No item details selected")}</dd>
            <dt>Condition evidence</dt><dd>${evidence}</dd>
            <dt>Review notes</dt><dd>${flags}</dd>
            <dt>Value estimate</dt><dd>${item.valuation_status === "estimated" || item.valuation_status === "provisional"
              ? esc(cleanLabel(item.valuation_status)) + " — inspect the full valuation record"
              : "Not estimated"}</dd>
          </dl>
          ${link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">Open Facebook listing ↗</a>` : ""}
        </div>
      </details>
    </div>
  </article>`;
}

function renderResults(items, emptyMessage = "No physical items match these filters.") {
  const search = document.getElementById("result-search").value.trim().toLowerCase();
  const grade = document.getElementById("result-grade").value;
  const sort = document.getElementById("result-sort").value;
  let selected = items.filter(item => {
    const haystack = [item.title, item.display_title, item.item_type,
      item.generated_description, item.refactored_description, item.location]
      .join(" ").toLowerCase();
    return (!search || haystack.includes(search)) &&
      (!grade || (item.condition_grade || "unknown") === grade);
  });
  if (sort === "distance") selected = selected.slice().sort((a, b) =>
    (a.distance_miles_approx ?? Infinity) - (b.distance_miles_approx ?? Infinity));
  if (sort === "title") selected = selected.slice().sort((a, b) =>
    (a.display_title || a.title).localeCompare(b.display_title || b.title));
  document.getElementById("visible-count").textContent = plural(selected.length, "item");
  document.getElementById("results-grid").innerHTML = selected.length
    ? selected.map(itemCard).join("")
    : `<div class="panel empty">${esc(items.length ? "No items match these page filters." : emptyMessage)}</div>`;
}

async function showScan(id) {
  try {
    if (!publicReadOnly && !publicHost && !selectedEmail()) {
      location.href = appPath("/");
      return;
    }
    const [scan, history, matches, account] = await Promise.all([
      api(`/api/scans/${encodeURIComponent(id)}`),
      publicReadOnly ? api("/api/scans") : accountApi("/api/scans"),
      publicReadOnly ? Promise.resolve({unread_count: 0}) : accountApi("/api/alerts"),
      publicReadOnly ? Promise.resolve(null) : accountApi("/api/account")
    ]);
    activeAccount = account;
    showAdminLink(account);
    if (!publicReadOnly && account?.role !== "admin" &&
        !history.scans.some(saved => saved.id === id)) {
      fillPicker(history.scans);
      app.innerHTML = `<div class="panel empty">This scan is not in the opened email profile.
        <p><a class="open-link" href="${appPath("/")}">Open your searches</a></p></div>`;
      return;
    }
    fillPicker(history.scans, id);
    showAlertCount(matches.unread_count);
    const running = ["queued", "running"].includes(scan.status);
    const priceExcluded = (scan.filtered_cards || []).filter(card =>
      (card.reason || "").includes("outside the saved") &&
      (card.reason || "").includes("range"));
    const noMatchNotice = scan.status === "complete" && !scan.result_count
      ? priceExcluded.length === scan.nearby_count && scan.nearby_count > 0
        ? `No results matched your $${esc(scan.watch_item.min_price)}–$${esc(scan.watch_item.max_price)} price range. All ${esc(plural(scan.nearby_count, "nearby listing"))} were outside that range. Open Ads filtered to see their prices.`
        : !scan.nearby_count
          ? `None of the ${esc(plural(scan.scanned_count || 0, "scanned card"))} were within the ${esc(scan.radius_miles)}-mile radius.`
          : `No listings passed every filter in this scan. Open Ads filtered to see why the ${esc(plural(scan.filtered_out_count || 0, "nearby listing"))} were excluded.`
      : "";
    app.innerHTML = `
      <a class="back-link" href="${appPath("/")}">← All scans</a>
      <section class="scan-header">
        <div><p class="eyebrow">Saved scan · ${esc(prettyDate(scan.created_at))}</p>
          <h1>${esc(scan.zip_code)} within ${esc(scan.radius_miles)} miles</h1>
          <p class="lead">Keywords: ${esc((scan.keywords || []).join(", "))}.${scan.watch_item ? ` Listed price: $${esc(scan.watch_item.min_price)}–$${esc(scan.watch_item.max_price)}.` : ""} Only physical items are shown. Locations use displayed seller towns, so distances are approximate.</p>
        </div>
        ${statusTag(scan.status)}
      </section>
      <div class="stats">
        <button class="panel stat stat-button" type="button" data-quick-list="scanned" aria-controls="quick-list-panel" aria-expanded="false"><span class="stat-label">Cards scanned · view list</span><strong class="stat-value">${esc(scan.scanned_count || 0)}</strong></button>
        <div class="panel stat"><span class="stat-label">Nearby cards</span><strong class="stat-value">${esc(scan.nearby_count || 0)}</strong></div>
        <div class="panel stat"><span class="stat-label">Items shown</span><strong class="stat-value">${esc(scan.result_count || 0)}</strong></div>
        <button class="panel stat stat-button" type="button" data-quick-list="filtered" aria-controls="quick-list-panel" aria-expanded="false"><span class="stat-label">Ads filtered · view list</span><strong class="stat-value">${esc(scan.filtered_out_count || 0)}</strong></button>
      </div>
      ${queueSection(queueForDisplay(history, account))}
      ${noMatchNotice ? `<p class="scan-notice">${noMatchNotice}</p>` : ""}
      <section class="panel quick-panel" id="quick-list-panel" hidden aria-live="polite"></section>
      ${scan.ai_error_count ? `<p class="scan-notice">AI photo classification timed out for ${esc(plural(scan.ai_error_count, "nearby card"))}. Open Ads filtered to see their names and seller descriptions.</p>` : ""}
      ${running ? `<section class="panel progress-panel" role="status"><h2>${esc(scan.stage)}</h2>
        <p>${scan.stage_progress
          ? `${esc(scan.stage_progress.label)}: ${esc(scan.stage_progress.processed)} of ${esc(scan.stage_progress.total)} checked. `
          : ""}Results appear here as the scan progresses. This page refreshes automatically.</p>
        <div class="progress-track" aria-hidden="true"><span${scan.stage_progress
          ? ` class="measured" style="width:${Math.round(100 * scan.stage_progress.processed / scan.stage_progress.total)}%"`
          : ""}></span></div></section>` : ""}
      ${scan.status === "failed" || scan.status === "interrupted"
        ? `<section class="panel progress-panel" role="alert"><h2>${esc(scan.stage)}</h2><p>${esc(scan.error || "Start a new scan to try again.")}</p></section>`
        : ""}
      <section aria-labelledby="results-title">
        <div class="results-heading">
          <div><p class="eyebrow">Collected nearby</p><h2 id="results-title">Items and AI results</h2>
            <p id="visible-count">${esc(plural(scan.listings.length, "item"))}</p></div>
          <div class="results-controls">
            <input id="result-search" type="search" placeholder="Search these results" aria-label="Search these results">
            <select id="result-grade" aria-label="Filter by condition">
              <option value="">All conditions</option>
              <option value="new">New</option><option value="like_new">Like new</option>
              <option value="good">Good</option><option value="fair">Fair</option>
              <option value="needs_repair">Needs repair</option><option value="unknown">Unknown</option>
            </select>
            <select id="result-sort" aria-label="Sort results">
              <option value="distance">Nearest first</option><option value="title">Title A–Z</option>
            </select>
          </div>
        </div>
        <div id="results-grid" class="results-grid"></div>
        <p class="caution">Photo and condition labels are suggestions. Open the listing and review the full photos and seller details before using an estimate.</p>
      </section>`;
    ["result-search", "result-grade", "result-sort"].forEach(name => {
      document.getElementById(name).addEventListener(
        name === "result-search" ? "input" : "change",
        () => renderResults(scan.listings));
    });
    document.querySelectorAll("[data-quick-list]").forEach(button => {
      button.addEventListener("click", () => {
        const panel = document.getElementById("quick-list-panel");
        const alreadyOpen = !panel.hidden && button.getAttribute("aria-expanded") === "true";
        document.querySelectorAll("[data-quick-list]").forEach(control =>
          control.setAttribute("aria-expanded", "false"));
        panel.hidden = alreadyOpen;
        if (alreadyOpen) return;
        button.setAttribute("aria-expanded", "true");
        const scanned = button.dataset.quickList === "scanned";
        const cards = scanned ? (scan.scanned_cards || []) : (scan.filtered_cards || []);
        const heading = scanned ? "Cards collected in this scan" : "Nearby cards left out of results";
        panel.innerHTML = `<div class="quick-heading"><h2>${heading}</h2><span>${esc(plural(cards.length, "card"))}</span></div>
          ${cards.length ? `<ol class="quick-list">${cards.map(quickCardRow).join("")}</ol>`
            : '<p class="form-help">No card details were saved for this list.</p>'}`;
      });
    });
    renderResults(scan.listings, scan.watch_item
      ? `No confirmed items within the saved $${scan.watch_item.min_price}–$${scan.watch_item.max_price} price range. Open Ads filtered to see why nearby cards were excluded.`
      : "No confirmed physical items in this scan. Open Ads filtered to review exclusions.");
    if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
    if (running && !pollTimer) {
      pollTimer = setInterval(() => {
        clearInterval(pollTimer);
        pollTimer = null;
        showScan(id);
      }, 4500);
    } else if (queueForDisplay(history, account).length && !pollTimer) {
      pollTimer = setTimeout(() => pollScanQueue(id), 4500);
    }
  } catch (failure) {
    app.innerHTML = `<div class="panel empty">Could not load this scan: ${esc(failure.message)}
      <p><a class="open-link" href="${appPath("/")}">Return to all scans</a></p></div>`;
  }
}

async function pollScanQueue(id) {
  pollTimer = null;
  try {
    const history = await accountApi("/api/scans");
    updateQueueSection(queueForDisplay(history, activeAccount));
    fillPicker(history.scans, id);
    if (queueForDisplay(history, activeAccount).length)
      pollTimer = setTimeout(() => pollScanQueue(id), 4500);
  } catch { /* The next page load can retry if Scout is temporarily unavailable. */ }
}

if (publicReadOnly && !scanPath) showPublicHome();
else if (pagePath === "/admin") showAdmin();
else if (pagePath === "/" && location.hash === "#admin") location.replace(appPath("/admin"));
else if (scanPath) showScan(scanPath[1]);
else showHome();

if (!publicReadOnly) setInterval(async () => {
  try { showAlertCount((await accountApi("/api/alerts")).unread_count); }
  catch { /* Keep the last count if the local server is unavailable. */ }
}, 30000);

if (!publicReadOnly) setInterval(async () => {
  const list = document.getElementById("home-history-list");
  const queue = document.getElementById("scan-queue");
  if ((!list && !queue) || (!publicHost && !selectedEmail())) return;
  try {
    const history = await accountApi("/api/scans");
    updateQueueSection(queueForDisplay(history, activeAccount));
    fillPicker(history.scans, scanPath ? scanPath[1] : "");
    if (!list) return;
    document.getElementById("home-history-count").textContent =
      plural(history.scans.length, "scan");
    list.innerHTML = history.scans.length ? history.scans.map(historyRow).join("")
      : '<div class="panel empty">No scans yet. Start with your ZIP code above.</div>';
  } catch { /* Keep the current archive until the next refresh. */ }
}, 8000);
