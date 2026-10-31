const app = document.getElementById("app");
app.addEventListener("click", event => {
  const button = event.target.closest(".item-photo-thumb");
  if (!button) return;
  const card = button.closest(".item-card");
  const main = card?.querySelector(".item-image");
  const link = card?.querySelector(".item-image-link");
  if (!main || !link) return;
  main.src = button.dataset.photoUrl;
  link.href = button.dataset.photoUrl;
  card.querySelectorAll(".item-photo-thumb").forEach(photo =>
    photo.setAttribute("aria-pressed", photo === button ? "true" : "false"));
});
const picker = document.getElementById("scan-picker");
const basePath = ["/notify", "/Facebooknotify"].find(prefix =>
  location.pathname === prefix || location.pathname.startsWith(`${prefix}/`)) || "";
const appPath = path => `${basePath}${path}`;
const pagePath = location.pathname.slice(basePath.length) || "/";
const scanPath = /^\/scan\/(scan-[a-z0-9-]+)$/.exec(pagePath);
const publicHost = Boolean(basePath) && !["127.0.0.1", "localhost"].includes(location.hostname);
const publicReadOnly = Boolean(basePath) && document.body.dataset.publicWrites !== "true";
let pollTimer = null;
let activeAccount = null;
let alertView = "inbox";
let alertPage = 1;
let alertRequestId = 0;
let feedbackByItem = {};
let feedbackScanId = "";
let feedbackEnabled = false;
const usedCandidatesByItem = new Map();
const tripInputsByItem = new Map();
const feedbackQuery = new URLSearchParams(location.search);
let pendingEmailFeedback = ["up", "down"].includes(feedbackQuery.get("feedback"))
  ? {vote: feedbackQuery.get("feedback"), listing: feedbackQuery.get("listing") || ""} : null;
const selectedEmail = () => publicHost ? (activeAccount?.email || "") :
  (localStorage.getItem("scout_demo_email") || "");

if (publicReadOnly) {
  for (const selector of [".header-alert-link", ".header-link"]) {
    document.querySelectorAll(selector).forEach(element => { element.style.display = "none"; });
  }
}
if (publicHost) for (const selector of [".admin-link", ".comps-link", ".general-comps-link"])
  document.querySelector(selector).style.display = "none";
const showAdminLink = account => {
  if (publicHost) for (const selector of [".admin-link", ".comps-link", ".general-comps-link"])
    document.querySelector(selector).style.display =
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
const tripMoney = value => new Intl.NumberFormat(undefined, {style: "currency", currency: "USD",
  minimumFractionDigits: Number.isInteger(value) ? 0 : 2, maximumFractionDigits: 2}).format(value);

function updateTripCalculator(panel, remember = false) {
  const fields = Object.fromEntries([...panel.querySelectorAll("[data-trip-field]")]
    .map(input => [input.dataset.tripField, input.value.trim()]));
  if (remember) tripInputsByItem.set(panel.dataset.tripId, fields);
  const result = panel.querySelector(".trip-result");
  const comparison = panel.querySelector(".trip-comparison");
  if (["roundTripMiles", "costPerMile", "repairCost", "otherCost"]
    .some(field => fields[field] === "")) {
    result.textContent = "Enter your round-trip driving miles and estimated costs.";
    comparison.textContent = "";
    comparison.classList.remove("trip-above");
    return;
  }
  const referenceMedian = panel.dataset.referenceMedian === "" ? null
    : Number(panel.dataset.referenceMedian);
  const estimate = globalThis.scoutTripEstimate({
    askingPrice: Number(panel.dataset.askingPrice),
    roundTripMiles: Number(fields.roundTripMiles),
    costPerMile: Number(fields.costPerMile),
    repairCost: Number(fields.repairCost),
    otherCost: Number(fields.otherCost),
    budget: fields.budget === "" ? null : Number(fields.budget),
    referenceMedian
  });
  if (!estimate.valid) {
    result.textContent = estimate.error;
    comparison.textContent = "";
    comparison.classList.remove("trip-above");
    return;
  }
  result.textContent = `Estimated total ${tripMoney(estimate.total)} = seller asks ${money(Number(panel.dataset.askingPrice))}` +
    ` + travel ${tripMoney(estimate.travelCost)} + repairs ${tripMoney(Number(fields.repairCost))}` +
    ` + other pickup costs ${tripMoney(Number(fields.otherCost))}.`;
  const budgetText = estimate.budgetDifference === null ? "" : estimate.budgetDifference >= 0
    ? ` ${tripMoney(estimate.budgetDifference)} within your budget.`
    : ` ${tripMoney(-estimate.budgetDifference)} over your budget.`;
  const medianText = estimate.referenceDifference === null
    ? "No compatible asking-price comparison is available for this listing."
    : estimate.referenceDifference === 0
      ? `Total equals the ${tripMoney(referenceMedian)} median asking price.`
      : `Total is ${tripMoney(Math.abs(estimate.referenceDifference))} ${estimate.referenceDifference > 0 ? "below" : "above"} the ${tripMoney(referenceMedian)} median asking price.`;
  comparison.textContent = `${medianText}${budgetText}`;
  comparison.classList.toggle("trip-above", estimate.referenceDifference < 0 ||
    (estimate.budgetDifference !== null && estimate.budgetDifference < 0));
}

app.addEventListener("input", event => {
  const panel = event.target.closest(".trip-check");
  if (panel && event.target.matches("[data-trip-field]")) updateTripCalculator(panel, true);
});
const applyBarPositions = root => {
  root.querySelectorAll("[data-bar-left], [data-bar-width]").forEach(element => {
    for (const [attribute, property] of [["barLeft", "left"], ["barWidth", "width"]]) {
      if (!(attribute in element.dataset)) continue;
      const value = Number(element.dataset[attribute]);
      if (Number.isFinite(value)) element.style[property] = `${Math.max(0, Math.min(100, value))}%`;
    }
  });
};
const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
const scanCountLabel = scan => {
  if (scan.status === "complete") {
    if (!Number.isInteger(scan.result_count)) return "Results unavailable";
    const count = scan.result_count;
    return `${count} ${count === 1 ? "item" : "items"} shown`;
  }
  if (scan.status === "running" && scan.result_count > 0) {
    return `${scan.result_count} early ${scan.result_count === 1 ? "item" : "items"} · analysis in progress`;
  }
  if (scan.status === "running" && scan.stage_progress) {
    const {label, processed, total} = scan.stage_progress;
    return `${label}: ${processed}/${total}`;
  }
  if (scan.status === "queued" && scan.retry_attempt) return "Retry scheduled";
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

app.addEventListener("click", async event => {
  const button = event.target.closest("[data-used-candidates-id]");
  if (!button || !scanPath) return;
  const listingId = button.dataset.usedCandidatesId;
  const output = button.closest(".retail-research")?.querySelector(".used-candidate-results");
  if (!listingId || !output) return;
  button.disabled = true;
  output.textContent = "Checking nearby used asking prices…";
  try {
    const result = await accountApi(`/api/scans/${encodeURIComponent(scanPath[1])}` +
      `/used-candidates/${encodeURIComponent(listingId)}`);
    usedCandidatesByItem.set(listingId, result);
    output.innerHTML = usedCandidateList(result);
  } catch (failure) {
    output.textContent = failure.message;
  } finally {
    button.disabled = false;
  }
});

app.addEventListener("click", async event => {
  const button = event.target.closest("[data-feedback-vote], [data-feedback-issue]");
  if (!button || !feedbackEnabled || !feedbackScanId) return;
  const panel = button.closest("[data-feedback-id]");
  const listingId = panel?.dataset.feedbackId;
  if (!listingId) return;
  const previous = feedbackByItem[listingId] || {};
  const vote = button.dataset.feedbackVote || previous.vote;
  const issue = button.dataset.feedbackIssue || (vote === "down" && previous.vote === "down"
    ? previous.issue || "" : "");
  const correction = vote === "down" && issue === previous.issue ? {
    corrected_item_type: previous.corrected_item_type || "",
    corrected_condition: previous.corrected_condition || "",
    corrected_price: previous.corrected_price,
    note: previous.note || ""
  } : {};
  const status = panel.querySelector("[data-feedback-status]");
  button.disabled = true;
  if (status) status.textContent = "Saving feedback…";
  try {
    const result = await accountApi(`/api/scans/${encodeURIComponent(feedbackScanId)}` +
      `/items/${encodeURIComponent(listingId)}/feedback`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({vote, issue, correction,
        ...(!publicHost ? {email: selectedEmail()} : {})})
    });
    feedbackByItem[listingId] = result.feedback;
    if (pendingEmailFeedback?.listing === listingId) {
      pendingEmailFeedback = null;
      history.replaceState(null, "", location.pathname + location.hash);
    }
    panel.outerHTML = feedbackPanel(listingId, result.feedback, "Feedback saved. Thank you.");
  } catch (failure) {
    button.disabled = false;
    if (status) status.textContent = failure.message;
  }
});

app.addEventListener("submit", async event => {
  const form = event.target.closest("[data-correction-form]");
  if (!form || !feedbackEnabled || !feedbackScanId) return;
  event.preventDefault();
  const panel = form.closest("[data-feedback-id]");
  const listingId = panel?.dataset.feedbackId;
  const previous = feedbackByItem[listingId] || {};
  if (!listingId || previous.vote !== "down" || !previous.issue) return;
  const button = form.querySelector('button[type="submit"]');
  const status = panel.querySelector("[data-feedback-status]");
  button.disabled = true;
  if (status) status.textContent = "Saving correction…";
  const correction = {note: form.querySelector("[data-correction-note]")?.value || ""};
  const field = form.querySelector("[data-correction-value]");
  if (field) correction[field.dataset.correctionValue] = field.value;
  try {
    const result = await accountApi(`/api/scans/${encodeURIComponent(feedbackScanId)}` +
      `/items/${encodeURIComponent(listingId)}/feedback`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({vote: "down", issue: previous.issue, correction,
        ...(!publicHost ? {email: selectedEmail()} : {})})
    });
    feedbackByItem[listingId] = result.feedback;
    panel.outerHTML = feedbackPanel(listingId, result.feedback,
      "Correction saved for review. Scout's estimate has not changed yet.");
  } catch (failure) {
    button.disabled = false;
    if (status) status.textContent = failure.message;
  }
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

function archiveContext(scan) {
  if (!activeAccount?.watch_items?.length ||
      !["scheduled", "incremental"].includes(scan.mode)) return "";
  const term = scan.watch_item?.term ||
    ((scan.keywords || []).length === 1 ? scan.keywords[0] : "");
  if (!term) return "";
  const current = activeAccount.watch_items.find(item =>
    item.term.toLocaleLowerCase() === String(term).toLocaleLowerCase());
  if (!current) return '<span class="archive-context">No longer watched</span>';
  const previous = scan.watch_item;
  if (previous && ["zip_code", "radius_miles", "min_price", "max_price"].some(
    field => previous[field] !== current[field])) {
    return '<span class="archive-context">Older search settings</span>';
  }
  return "";
}

function historyRow(scan, archived = false) {
  return `<article class="panel history-row">
    <div>
      <h3>${esc(scan.zip_code)} <span aria-hidden="true">·</span> ${esc(scan.radius_miles)} mile radius</h3>
      <div class="meta-line">
        <span>${esc(prettyDate(scan.created_at))}</span>
        <span>${esc((scan.keywords || []).join(", "))}</span>
        ${scan.collection_new_only || scan.mode === "incremental" ? `<span>${scan.mode === "incremental" ? "Fast" : "Daily"} check · new or updated cards</span>` : ""}
        ${scan.watch_item ? `<span>Listed $${esc(scan.watch_item.min_price)}–$${esc(scan.watch_item.max_price)}</span>` : ""}
        ${archived ? archiveContext(scan) : ""}
        ${statusTag(scan.status)}
      </div>
      ${scan.status === "running" && scan.early_items?.length
        ? `<div class="early-items">Early identifications: ${scan.early_items.map(item =>
            `<a href="${appPath(`/scan/${encodeURIComponent(scan.id)}#item-${encodeURIComponent(item.id)}`)}">${esc(item.title)}</a>`).join(" · ")}</div>`
        : ""}
    </div>
    <div class="scan-count">${esc(scanCountLabel(scan))}</div>
    ${scan.id
      ? `<a class="open-link" href="${appPath(`/scan/${encodeURIComponent(scan.id)}`)}">Open results →</a>`
      : `<span class="queue-waiting">${scan.retry_at
          ? `Retry at ${esc(prettyDate(scan.retry_at))}` : "Waiting its turn"}</span>`}
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

function activityContent(activity) {
  if (!activity.length) return '<div class="panel empty">Save a keyword to see its scan activity.</div>';
  return activity.map(item => {
    const state = item.status === "not_started" ? "Awaiting first check" :
      item.status === "queued" && item.retry_attempt ? "Retry queued" :
      item.status === "queued" ? "Queued" :
      item.status === "running" ? "Checking now" :
      item.status === "complete" ? "Checked" : "Needs attention";
    const stateClass = ["complete", "running", "queued"].includes(item.status)
      ? item.status : item.status === "not_started" ? "idle" : "failed";
    const when = item.last_attempt_at
      ? `Last attempt ${prettyDate(item.last_attempt_at)}` : "No scheduled check yet";
    const checked = Number.isInteger(item.cards_checked)
      ? `${plural(item.cards_checked, "card")} seen` : "";
    const fresh = Number.isInteger(item.new_cards_reviewed)
      ? `${plural(item.new_cards_reviewed, "new card")} reviewed` : "";
    const details = [when, checked, fresh].filter(Boolean).join(" · ");
    const olderSuccess = item.error && item.last_success_at
      ? `<p class="activity-note">Last completed check: ${esc(prettyDate(item.last_success_at))}</p>` : "";
    const error = item.error
      ? `<p class="activity-error">${esc(item.error)}</p>` : "";
    const target = item.scan_id || item.last_scan_id;
    return `<article class="panel activity-card">
      <div class="activity-card-head"><div><h3>${esc(item.term)}</h3>
        <p>${esc(item.zip_code)} · ${esc(item.radius_miles)} mi · Listed $${esc(item.min_price)}–$${esc(item.max_price)}${item.fast ? " · 30-minute checks" : ""}</p></div>
        <span class="activity-state ${stateClass}">${esc(state)}</span></div>
      <p class="activity-detail">${esc(details)}</p>${error}${olderSuccess}
      ${target ? `<a class="open-link" href="${appPath(`/scan/${encodeURIComponent(target)}`)}">View latest scan →</a>` : ""}
    </article>`;
  }).join("");
}

function updateActivitySection(activity) {
  const list = document.getElementById("watch-activity-list");
  if (list) list.innerHTML = activityContent(activity || []);
}

function coverageDepths(rows, field) {
  return (rows || []).map(row => `${row.depth} cards: ${field(row)}`).join(" · ");
}

function effectiveCoveragePilot(pilot, account) {
  if (!pilot || pilot.status !== "running" || !account || pilot.silent) return pilot;
  const priority = (account.watch_items || []).find(item => item.fast);
  return {...pilot, paused: pilot.paused ||
    priority?.term?.toLowerCase() !== (pilot.term || "mower").toLowerCase()};
}

function coveragePilotContent(pilot, scans) {
  if (!pilot) return "";
  if (pilot.status === "cancelled") return `<div class="pilot-heading"><p class="eyebrow">Previous experiment</p>
    <h2 id="coverage-pilot-title">Mower depth checks stopped</h2></div>
    <p>${esc(pilot.stop_reason || "Scout stopped repeating this search.")}</p>`;
  const latest = (scans || []).find(scan => scan.coverage_probe);
  const probe = pilot.latest_probe || latest?.coverage_probe;
  const heading = pilot.paused ? "Mower depth comparison is paused" :
    pilot.status === "running" ? "Mower depth comparison is running" :
    "Mower depth comparison finished";
  const result = pilot.report;
  const summary = result
    ? `<p>${esc(result.completed_runs)} of ${esc(result.runs)} checks completed. ${esc(
        coverageDepths(result.depths, row => `${row.unique_new_ids} distinct new IDs${
          row.median_seconds_to_depth == null ? "" : ` · median ${row.median_seconds_to_depth}s`}`))}.</p>
       <strong>Selected depth: ${esc(result.chosen_depth)} cards</strong>
       <p class="pilot-note">${esc(result.note)}</p>`
    : `<p>${pilot.paused ? "Mower is no longer the saved priority keyword, so no pilot checks are scheduled. " :
         pilot.silent ? "This comparison runs separately from your saved plan and sends no alerts or email. " : ""}
       Comparing the first 12, 40, and 80 visible cards in each quick check until
       ${esc(prettyDate(pilot.ends_at))}. One collection measures all three depths.
       ${pilot.silent ? "These checks use card IDs only, with no image downloads or AI review." :
         "Only unseen cards receive photo downloads and AI review."}</p>
       ${probe ? `<p><strong>Latest check:</strong> ${esc(probe.cards_seen)} cards seen · ${esc(
          coverageDepths(probe.depths, row => `${(row.new_listing_ids || []).length} unseen${
            row.seconds_to_depth == null ? "" : ` · ${row.seconds_to_depth}s`}`))}.${
          probe.seconds_to_stop == null ? "" : ` Search stopped after ${esc(probe.seconds_to_stop)}s.`}</p>` :
         '<p class="pilot-note">Waiting for the next mower quick check.</p>'}`;
  return `<div class="pilot-heading"><p class="eyebrow">Finding more listings</p>
      <h2 id="coverage-pilot-title">${esc(heading)}</h2></div>${summary}
    <p class="pilot-note">Counts compare cards Scout could see in these searches, not every listing on Marketplace.</p>`;
}

function updateCoveragePilotSection(history) {
  const panel = document.getElementById("coverage-pilot");
  if (!panel) return;
  panel.hidden = !history.coverage_pilot;
  if (history.coverage_pilot)
    panel.innerHTML = coveragePilotContent(
      effectiveCoveragePilot(history.coverage_pilot, activeAccount), history.scans);
}

function coverageProbePanel(probe) {
  if (!probe) return "";
  return `<section class="panel pilot-panel" aria-labelledby="scan-coverage-title">
    <p class="eyebrow">Depth comparison</p><h2 id="scan-coverage-title">What this quick check found</h2>
    <p>Scout saw ${esc(plural(probe.cards_seen, "visible card"))}.
      ${esc(coverageDepths(probe.depths, row => `${(row.new_listing_ids || []).length} unseen${
        row.seconds_to_depth == null ? "" : ` · ${row.seconds_to_depth}s`}`))}.</p>
    <p class="pilot-note">${esc(({
      item_limit: "Stopped at the 80-card cap.",
      scroll_limit: "Stopped after the planned scroll depth.",
      no_new_cards_after_five_scrolls: "Stopped after five scrolls revealed no additional cards."
    })[probe.stop_reason] || "The visible search ended before every depth was reached.")}</p>
    <p class="pilot-note">These are nested groups from one collection. Known cards did not need another AI review.</p>
  </section>`;
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

function watchRows(items, locks, globals, fastAvailable) {
  return items.map((item, index) => `<div class="watch-item-row" data-watch-term="${esc(item.term)}">
    <div class="watch-item-name"><strong>${esc(item.term)}</strong><small>Scan ${index + 1}</small>
      <label class="fast-check"><input data-watch-fast type="checkbox" ${item.fast ? "checked" : ""}
        ${fastAvailable ? "" : "disabled"}> 30-minute checks</label></div>
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

function outsideCardRow(card) {
  const link = sourceUrl(card.url);
  return `<li class="quick-row"><strong>${esc(card.title || "Untitled listing")}</strong>
    <p>${esc(card.location || "Location unavailable")} · about ${esc(Number(card.distance_miles).toFixed(1))} mi from your ZIP · ${esc(money(card.asking_price))}</p>
    ${card.near_boundary ? '<small>Near the radius edge; seller town gives only an approximate location.</small>' : ""}
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
        <div><span class="stat-label">30-minute priority check</span><strong>${esc((profile.watch_items || []).find(item => item.fast)?.term || "Off")}</strong></div>
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
    ${overview.quality ? `<section class="admin-section" aria-labelledby="quality-title">
      <div class="section-heading"><div><p class="eyebrow">Reliability</p><h2 id="quality-title">Results to review</h2></div></div>
      <div class="panel quality-overview">
        <p>Across the latest ${esc(overview.quality.scan_count)} completed scans: ${esc(overview.quality.item_count)} distinct items · ${esc(overview.quality.unknown_condition_count)} without a condition grade · ${esc(overview.quality.unpriced_count)} without a price estimate.</p>
        ${overview.quality.feedback_metrics ? `<h3>Review feedback by category</h3>
          <p>${esc(overview.quality.feedback_metrics.reviewed)} current votes on ${esc(overview.quality.feedback_metrics.unique_listings)} listings · ${esc(overview.quality.feedback_metrics.confirmed)} marked right · ${esc(overview.quality.feedback_metrics.corrections)} marked for correction.${overview.quality.feedback_metrics.sample_sufficient
            ? ` ${esc(Math.round(100 * overview.quality.feedback_metrics.confirmed / overview.quality.feedback_metrics.reviewed))}% of submitted votes are positive; this is not an accuracy score for all listings.`
            : " Fewer than 20 reviews: no reliability percentage yet."}</p>
          ${overview.quality.feedback_metrics.by_category.length ? `<ul class="quality-list">${overview.quality.feedback_metrics.by_category.map(row =>
            `<li><strong>${esc(cleanLabel(row.category))}</strong><span>${esc(row.confirmed)} right · ${esc(row.corrections)} corrections · ${esc(row.reviewed)} reviewed${Object.entries(row.issues || {}).map(([issue, count]) => ` · ${esc(cleanLabel(issue))}: ${esc(count)}`).join("")}</span></li>`).join("")}</ul>` : ""}
          ${overview.quality.feedback_metrics.by_issue.length ? `<p>Correction types: ${overview.quality.feedback_metrics.by_issue.map(row =>
            `${esc(cleanLabel(row.issue))} ${esc(row.count)}`).join(" · ")}</p>` : ""}
          <p class="form-help">Votes are voluntary and can be changed. These counts describe submitted feedback, not an independent test set.</p>` : ""}
        <h3>Corrections from people · ${esc(overview.quality.correction_count)}</h3>
        ${overview.quality.corrections.length ? `<ul class="quality-list">${overview.quality.corrections.map(row => {
          const corrected = row.issue === "wrong_item" ? row.corrected_item_type
            : row.issue === "wrong_condition" ? cleanLabel(row.corrected_condition)
            : row.issue === "wrong_price" && typeof row.corrected_price === "number" ? money(row.corrected_price)
            : "";
          return `<li><a href="${appPath(`/scan/${encodeURIComponent(row.scan_id)}#item-${encodeURIComponent(row.listing_id)}`)}">${esc(row.title)}</a>
            <span>${esc(cleanLabel(row.issue || "unspecified"))}${corrected ? ` · correction: ${esc(corrected)}` : ""}${row.note ? ` · ${esc(row.note)}` : ""}</span></li>`;
        }).join("")}</ul>` : '<p>No correction votes yet.</p>'}
        <h3>Uncertain recent items</h3>
        ${overview.quality.review_items.length ? `<ul class="quality-list">${overview.quality.review_items.map(row =>
          `<li><a href="${appPath(`/scan/${encodeURIComponent(row.scan_id)}#item-${encodeURIComponent(row.id)}`)}">${esc(row.title)}</a>
            <span>${row.condition_grade === "unknown" || !row.condition_grade ? "Condition unknown" : ""}${row.valuation_reason ? `${row.condition_grade === "unknown" || !row.condition_grade ? " · " : ""}Price: ${esc(cleanLabel(row.valuation_reason))}` : ""}</span></li>`).join("")}</ul>` : '<p>No uncertain items in these scans.</p>'}
        <p class="form-help">Corrections are saved for human review and do not automatically change model results.</p>
      </div>
    </section>` : ""}
    <section class="admin-section" aria-labelledby="admin-prices-title">
      <div class="section-heading"><div><p class="eyebrow">Asking prices</p><h2 id="admin-prices-title">Price samples</h2></div></div>
      <div class="panel"><p>Review local listing photos, seller descriptions, and the median asking prices used for estimates.</p>
        <p><a class="open-link" href="${appPath("/comparables/general")}">Appliances, furniture, mowers, and scooters →</a></p>
        <p><a class="open-link" href="${appPath("/comparables/couches")}">Couch prices →</a></p>
        <p><a class="open-link" href="${appPath("/price-research")}">Price research queue →</a></p></div>
    </section>
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
          ? history.scans.map(scan => historyRow(scan, true)).join("")
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
  const facebook = sourceUrl(alert.facebook_url);
  const image = typeof alert.image_url === "string" &&
    /^\/api\/scans\/scan-[a-z0-9-]+\/images\/\d+(?:\/[0-3])?$/.test(alert.image_url)
      ? `<img class="alert-photo" src="${esc(appPath(alert.image_url))}" alt="Photo of ${esc(alert.title)}" loading="lazy">` : "";
  const price = typeof alert.price === "number" && Number.isFinite(alert.price)
    ? `<strong class="alert-price">${esc(money(alert.price))}</strong>` : "";
  const distance = typeof alert.distance_miles === "number"
    ? ` · about ${esc(alert.distance_miles)} mi away` : "";
  const grade = alert.condition || alert.condition_grade;
  const condition = alert.pet_listing ? "Condition not applicable" :
    grade && grade !== "unknown"
      ? `Condition: ${cleanLabel(grade)}` : "Condition unconfirmed";
  const itemType = alert.item_type && alert.item_type !== "unknown"
    ? cleanLabel(alert.item_type) : "Item type unconfirmed";
  return `<article class="panel alert-row ${alert.read_at ? "" : "unread"}">
    ${image}<div class="alert-copy">
      <div class="alert-title-line"><h3>${esc(alert.title)}</h3>${price}</div>
      <p>${esc(alert.location || alert.zip_code)}${distance} · ${esc(prettyDate(alert.matched_at))}</p>
      <p class="alert-facts">${esc(itemType)} · ${esc(condition)}</p>
      ${alert.comparison_label ? `<p class="alert-comparison">${esc(alert.comparison_label)}</p>` : ""}
      ${alert.reason ? `<p class="alert-reason">${esc(alert.reason)}</p>` : ""}
      ${alert.price_note ? `<p class="alert-price-note">${esc(alert.price_note)}</p>` : ""}
      <div class="alert-actions"><a class="open-link" href="${esc(target)}" data-read-alert="${esc(alert.id)}">View Scout analysis →</a>
      ${facebook ? `<a class="open-link" href="${esc(facebook)}" target="_blank" rel="noopener noreferrer">Open on Facebook ↗</a>` : ""}
      ${alertView === "saved" || alertView === "hidden" ? `<button type="button" class="alert-triage" data-alert-triage="inbox" data-alert-id="${esc(alert.id)}">Move to Inbox</button>` :
        `<button type="button" class="alert-triage" data-alert-triage="saved" data-alert-id="${esc(alert.id)}">Save</button>`}
      ${alertView === "hidden" ? "" : `<button type="button" class="alert-triage alert-hide" data-alert-triage="hidden" data-alert-id="${esc(alert.id)}">Hide</button>`}</div>
    </div>
  </article>`;
}

function alertSectionContent(matches) {
  const counts = matches.triage_counts || {inbox: matches.alerts.length, saved: 0, hidden: 0};
  const pageSize = Number(matches.limit) || 30;
  const total = Number(counts[alertView]) || 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const labels = {inbox: "Inbox", saved: "Saved", hidden: "Hidden"};
  const empty = {inbox: "New matching items will appear here after a scan.",
    saved: "Save a promising listing to find it here later.",
    hidden: "Hidden listings will appear here if you need to restore one."};
  return `<div class="section-heading"><div><p class="eyebrow">Matches</p><h2 id="alerts-title">Alerts</h2></div>
      <p id="alerts-unread">${esc(plural(matches.unread_count, "unread alert"))}</p></div>
    <div class="alert-toolbar"><div class="alert-view-tabs" role="group" aria-label="Filter alerts">
      ${Object.entries(labels).map(([view, label]) => `<button type="button" class="alert-view-tab ${alertView === view ? "active" : ""}"
        data-alert-view="${view}" aria-pressed="${alertView === view}">${label} <span>${esc(counts[view] || 0)}</span></button>`).join("")}</div>
      ${alertView === "inbox" && Number(counts.inbox) > 0
        ? '<button type="button" class="alert-hide-all" data-alert-hide-all aria-label="Hide all inbox alerts">Hide all alerts</button>' : ""}</div>
    <p class="form-error" id="alerts-error" role="alert"></p>
    <div class="alerts-list">${matches.alerts.length ? matches.alerts.map(alertRow).join("")
      : `<div class="panel empty">${empty[alertView]}</div>`}</div>
    ${total > pageSize ? `<div class="alert-pagination" aria-label="Alert pages">
      <button type="button" data-alert-page="previous" ${alertPage <= 1 ? "disabled" : ""}>← Newer</button>
      <span>Page ${alertPage} of ${pageCount} · ${total} alerts</span>
      <button type="button" data-alert-page="next" ${alertPage >= pageCount ? "disabled" : ""}>Older →</button>
    </div>` : ""}`;
}

async function refreshAlerts() {
  const requestId = ++alertRequestId;
  const view = alertView;
  let page = alertPage;
  let matches = await accountApi(`/api/alerts?view=${view}&page=${page}`);
  if (requestId !== alertRequestId) return;
  if (alertPage > 1 && !matches.alerts.length) {
    page -= 1;
    alertPage = page;
    matches = await accountApi(`/api/alerts?view=${view}&page=${page}`);
    if (requestId !== alertRequestId) return;
  }
  showAlertCount(matches.unread_count);
  const topHideAll = document.querySelector("[data-top-hide-all]");
  if (topHideAll) topHideAll.disabled = !(Number(matches.triage_counts?.inbox) > 0);
  const section = document.querySelector(".alerts-section");
  if (section) section.innerHTML = alertSectionContent(matches);
}

app.addEventListener("click", async event => {
  const hideAll = event.target.closest("[data-alert-hide-all]");
  if (hideAll) {
    hideAll.disabled = true;
    try {
      await accountApi("/api/alerts/hide-all", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(!publicHost ? {email: selectedEmail()} : {})
      });
      alertPage = 1;
      await refreshAlerts();
    } catch (failure) {
      const error = document.getElementById("alerts-error");
      if (error) error.textContent = failure.message;
      hideAll.disabled = false;
    }
    return;
  }
  const viewButton = event.target.closest("[data-alert-view]");
  if (viewButton) {
    alertView = viewButton.dataset.alertView;
    alertPage = 1;
    try { await refreshAlerts(); }
    catch (failure) { document.getElementById("alerts-error").textContent = failure.message; }
    return;
  }
  const pageButton = event.target.closest("[data-alert-page]");
  if (pageButton && !pageButton.disabled) {
    alertPage += pageButton.dataset.alertPage === "next" ? 1 : -1;
    try { await refreshAlerts(); document.getElementById("alerts-title")?.scrollIntoView(); }
    catch (failure) { document.getElementById("alerts-error").textContent = failure.message; }
    return;
  }
  const triageButton = event.target.closest("[data-alert-triage]");
  if (triageButton) {
    triageButton.disabled = true;
    try {
      await api(`/api/alerts/${encodeURIComponent(triageButton.dataset.alertId)}/triage`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({status: triageButton.dataset.alertTriage,
          ...(!publicHost ? {email: selectedEmail()} : {})})
      });
      await refreshAlerts();
    } catch (failure) {
      const error = document.getElementById("alerts-error");
      if (error) error.textContent = failure.message;
      triageButton.disabled = false;
    }
    return;
  }
  const readLink = event.target.closest("[data-read-alert]");
  if (readLink) {
    event.preventDefault();
    try {
      await api(`/api/alerts/${encodeURIComponent(readLink.dataset.readAlert)}/read`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(!publicHost ? {email: selectedEmail()} : {})
      });
    } catch { /* The saved result still opens if marking read fails. */ }
    location.href = readLink.href;
  }
});

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
          alertView = "inbox";
          alertPage = 1;
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
      accountApi("/api/account"), accountApi(`/api/alerts?view=${alertView}&page=${alertPage}`)
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
    const priorityTerm = (account.watch_items || []).find(item => item.fast)?.term;
    const nextMessage = account.configured
        ? `Next full scan: ${prettyDateInZone(account.next_run_at, account.timezone)} (${account.timezone})`
        : "Save a plan to schedule scans";
    const nextFastMessage = priorityTerm && account.next_fast_run_at
      ? `${priorityTerm} quick check: ${prettyDateInZone(account.next_fast_run_at, account.timezone)}` : "";
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
      <div class="alert-top-action">
        <p>Clear your Inbox in one step. You can restore alerts from Hidden.</p>
        <button type="button" class="button button-secondary" data-alert-hide-all data-top-hide-all
          aria-label="Hide all inbox alerts" ${Number(matches.triage_counts?.inbox) > 0 ? "" : "disabled"}>Hide all alerts</button>
      </div>
      <section class="alerts-section" aria-labelledby="alerts-title">${alertSectionContent(matches)}</section>
      <section class="panel pilot-panel" id="coverage-pilot" aria-labelledby="coverage-pilot-title"
        ${history.coverage_pilot ? "" : "hidden"}>${coveragePilotContent(
          effectiveCoveragePilot(history.coverage_pilot, account), history.scans)}</section>
      <section class="activity-section" aria-labelledby="watch-activity-title">
        <div class="section-heading"><div><p class="eyebrow">Your keywords</p>
          <h2 id="watch-activity-title">Search activity</h2></div>
          <p>Latest checks of visible Marketplace cards.<br><a class="open-link" href="${appPath("/health")}">See scan health and benchmark →</a></p></div>
        <div class="activity-grid" id="watch-activity-list">${activityContent(history.activity || [])}</div>
      </section>
      <section class="plan-section" aria-labelledby="plan-title">
        <div class="section-heading"><div><p class="eyebrow">Your plan</p><h2 id="plan-title">Set up your searches</h2></div>
          <p>${esc(nextMessage)}${nextFastMessage ? `<br>${esc(nextFastMessage)}` : ""}</p></div>
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
                <span><strong>Plus preview</strong><small>Proposed $10 / month</small><span>Each keyword gets full scans at 7:00 a.m. and noon. One priority keyword can also check for new listings every 30 minutes.</span></span></label>
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
              <p class="form-help">Plus preview: select one priority keyword for quick checks of cards new to that search. Quick checks wait for the current scan to finish and pause during the 7 a.m. and noon full scans.</p>
              <div class="watch-locks" role="group" aria-label="Apply defaults to every keyword">
                ${[["zip_code", "ZIP code"], ["radius_miles", "Radius"], ["min_price", "Minimum price"], ["max_price", "Maximum price"]]
                  .map(([key, label]) => `<label><input type="checkbox" data-global-lock="${key}" ${locks[key] ? "checked" : ""}> Lock ${label}</label>`).join("")}
              </div>
              <div class="watch-items" id="watch-items">${watchRows(watchDraft, locks, defaults, account.plan === "plus_preview")}</div>
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
      <section class="history" aria-labelledby="history-title">
        <div class="section-heading"><div><p class="eyebrow">Archive</p><h2 id="history-title">Saved scans</h2></div>
          <p id="home-history-count">${esc(plural(history.scans.length, "scan"))}</p></div>
        <div class="history-list" id="home-history-list">${history.scans.length
          ? history.scans.map(scan => historyRow(scan, true)).join("")
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
          alertView = "inbox";
          alertPage = 1;
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
      const current = {term: row.dataset.watchTerm,
        fast: row.querySelector("[data-watch-fast]").checked};
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
      document.getElementById("watch-items").innerHTML = watchRows(watchDraft, currentLocks, global,
        document.querySelector('input[name="plan"]:checked').value === "plus_preview");
    };
    document.getElementById("watch-items").addEventListener("change", event => {
      if (!event.target.matches("[data-watch-fast]") || !event.target.checked) return;
      document.querySelectorAll("[data-watch-fast]").forEach(input => {
        if (input !== event.target) input.checked = false;
      });
    });
    document.querySelectorAll('input[name="plan"]').forEach(input =>
      input.addEventListener("change", syncWatchRows));
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
            watch_items: readWatchRows().map(item => ({...item,
              fast: document.querySelector('input[name="plan"]:checked').value === "plus_preview" && item.fast})),
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

function comparableUrl(value) {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    return url.protocol === "https:" &&
      (["facebook.com", "www.facebook.com", "craigslist.org"].includes(host) ||
        host.endsWith(".craigslist.org")) ? url.href : "";
  } catch { return ""; }
}

function comparisonRequirements(valuation) {
  if (!valuation?.product_type || !Array.isArray(valuation.feature_clues_required)) return "";
  const features = valuation.feature_clues_required.map(feature =>
    feature.startsWith("named_") ? `${feature.slice(6)} named in the listing` :
    feature === "handcrafted" ? "handmade construction" :
    feature === "chaise" ? "a chaise" :
    feature === "reclining" ? "reclining function" : cleanLabel(feature));
  const materials = (valuation.material_clues_required || []).map(cleanLabel);
  return [cleanLabel(valuation.product_type), ...materials, ...features].join(" · ");
}

function comparableEvidence(valuation) {
  const rows = valuation?.comparable_evidence;
  if (!Array.isArray(rows) || !rows.length) return "";
  const total = Number(valuation.comparable_count) || rows.length;
  const scope = referenceScope(valuation);
  return `<details class="comparable-evidence">
    <summary>Check ${esc(total)} price ${total === 1 ? "comparison" : "comparisons"} ${esc(scope.detail)}</summary>
    <div class="comparable-evidence-body">
      <p>These are seller asking prices observed within the last year, not confirmed sale prices. ${esc(valuation.basis || "Comparable listings selected by item type and condition.")}${comparisonRequirements(valuation) ? ` Required match: ${esc(comparisonRequirements(valuation))}.` : ""}</p>
      ${rows.length < total ? `<p>Showing ${esc(rows.length)} prices across the sample of ${esc(total)}.</p>` : ""}
      <ol>${rows.map(row => {
        const href = comparableUrl(row.source_url);
        const label = row.title || "Untitled listing";
        const title = href
          ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>`
          : `<span>${esc(label)}</span>`;
        const age = row.observed_at ? prettyDate(row.observed_at) : "Date unavailable";
        const distance = Number.isFinite(row.distance_miles)
          ? ` · ≈ ${row.distance_miles.toFixed(1)} mi` : "";
        return `<li><div><strong>${esc(money(row.asking_price))}</strong>${title}</div>
          <small>${esc(row.location || "Location unavailable")}${esc(distance)} · ${esc(cleanLabel(row.condition))} · ${esc(age)}</small></li>`;
      }).join("")}</ol>
      <p>Open a listing to check its size, model, included parts, and condition before relying on the price rating.</p>
    </div>
  </details>`;
}

function bundleBreakdown(valuation) {
  const parts = valuation?.bundle_components;
  if (!Array.isArray(parts) || !parts.length) return "";
  return `<section class="bundle-breakdown" aria-label="Bundle price breakdown">
    <h4>Priced as separate items</h4>
    <ul>${parts.map(part => {
      const name = part.label || cleanLabel(part.product_type);
      const ready = Number.isFinite(part.unit_low) && Number.isFinite(part.unit_high);
      const detail = ready
        ? `${money(part.unit_low)}–${money(part.unit_high)} each · ${money(part.unit_low * part.quantity)}–${money(part.unit_high * part.quantity)} for ${part.quantity}`
        : `${part.comparable_count || 0} of 3 single-item prices found`;
      return `<li><div><strong>${esc(part.quantity ?? "?")} × ${esc(name)}</strong><span>${esc(detail)}</span></div>
        ${comparableEvidence({comparable_evidence: part.comparable_evidence,
          comparable_count: part.comparable_count,
          reference_scope_miles: valuation.reference_scope_miles,
          basis: `Individual ${name.toLowerCase()} asking prices, not bundle listings.`})}</li>`;
    }).join("")}</ul>
    <p>The combined range adds individual asking prices. It does not assume a discount for buying everything together or verify each piece's condition.</p>
  </section>`;
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
  mixed_item_lot: "The seller offers several different items. Their condition and value must be checked separately.",
  description_missing: "The seller description was unavailable.",
  second_opinion_disagreement: "The two local models disagree. Check the original photos and seller details."
};

function referenceScope(valuation) {
  const miles = Number(valuation?.reference_scope_miles);
  if (!Number.isFinite(miles) || miles <= 0)
    return {label: "Local", detail: "nearby"};
  return {label: miles <= 60 ? "Local" : miles <= 250 ? "Regional" : "Broader area",
    detail: `within about ${miles} miles`};
}

function priceGuide(item) {
  const position = item.valuation?.price_category;
  const itemized = item.valuation?.price_type === "itemized_asking_range";
  const scope = referenceScope(item.valuation);
  const codes = ["best", "good", "average", "slightly_above", "high"];
  if (!position || !codes.includes(position.code) ||
      !Array.isArray(position.cutoffs) || position.cutoffs.length !== 4 ||
      !position.cutoffs.every(Number.isFinite)) return "";
  const cutoffs = position.cutoffs;
  const names = ["Best", "Good", "Average", "Slightly above", "High"];
  const ranges = [`≤ ${money(cutoffs[0])}`,
    `${money(cutoffs[0])}–${money(cutoffs[1])}`,
    `${money(cutoffs[1])}–${money(cutoffs[2])}`,
    `${money(cutoffs[2])}–${money(cutoffs[3])}`,
    `> ${money(cutoffs[3])}`];
  const marker = Number.isFinite(position.bar_position_percent)
    ? Math.max(3, Math.min(97, position.bar_position_percent)) : 50;
  return `<div class="price-guide" role="group" aria-label="Price guide: ${esc(position.label)}. Seller asks ${esc(money(position.seller_asking_price))}. ${itemized ? "Based on a sum of separate item asking prices" : `Based on ${esc(position.comparison_count)} similar asking prices ${esc(scope.detail)}`}.">
    <div class="price-guide-heading"><strong>${esc(position.label)}</strong><span>Seller asks ${esc(money(position.seller_asking_price))}</span></div>
    <div class="price-guide-track" aria-hidden="true">${codes.map(code => `<span class="price-guide-segment ${code}"></span>`).join("")}
      <span class="price-guide-marker" data-bar-left="${marker}"></span></div>
    <div class="price-guide-legend">${names.map((name, i) => `<div class="${codes[i] === position.code ? "selected" : ""}"><strong>${esc(name)}</strong><small>${esc(ranges[i])}</small></div>`).join("")}</div>
    <p>${itemized
      ? `Left means cheaper than the sum of separately priced items · at least ${esc(item.valuation.comparable_count)} prices per item ${esc(scope.detail)} from the last year · low confidence`
      : `Left means cheaper than similar listings · ${esc(position.comparison_count)} asking prices ${esc(scope.detail)} from the last year · ${esc(position.confidence)} confidence`}</p>
    ${!itemized && item.valuation?.unknown_comparable_conditions
      ? `<p>${esc(item.valuation.unknown_comparable_conditions)} comparison ${item.valuation.unknown_comparable_conditions === 1 ? "listing has" : "listings have"} an unconfirmed condition; the price rating is less certain.</p>`
      : !itemized && item.valuation?.condition_assumption === "unknown"
        ? "<p>This item's condition is unconfirmed, so the price rating is less certain.</p>" : ""}
  </div>`;
}

function askingContextGuide(item) {
  const context = item.valuation?.asking_context;
  const scope = referenceScope(item.valuation);
  const ask = Number(item.asking_price);
  if (!context || ![ask, context.low, context.high, context.midpoint].every(Number.isFinite) ||
      context.comparable_count < 3) return "";
  const extent = Math.max(10, context.high - context.low, ask * 0.15);
  const floor = Math.max(0, Math.min(ask, context.low) - extent * 0.25);
  const ceiling = Math.max(ask, context.high) + extent * 0.25;
  const percent = value => Math.max(2, Math.min(98,
    (value - floor) / (ceiling - floor) * 100));
  const low = percent(context.low);
  const high = percent(context.high);
  const midpoint = percent(context.midpoint);
  const asking = percent(ask);
  return `<div class="asking-context-guide" role="group" aria-label="Unadjusted asking-price comparison ${esc(scope.detail)}. Seller asks ${esc(money(ask))}. Median ${esc(money(context.midpoint))} from ${esc(context.comparable_count)} listings.">
    <div class="price-guide-heading"><strong>${esc(scope.label)} asking-price comparison</strong><span>${esc(context.comparable_count)} ${esc(context.basis)} listings</span></div>
    <div class="asking-context-track" aria-hidden="true">
      <span class="asking-context-band" data-bar-left="${low}" data-bar-width="${high - low}"></span>
      <span class="asking-context-median" data-bar-left="${midpoint}"></span>
      <span class="asking-context-marker" data-bar-left="${asking}"></span>
    </div>
    <div class="asking-context-labels"><span>Seller asks ${esc(money(ask))}</span><span>Median ${esc(money(context.midpoint))}</span></div>
    <p>Shaded range: middle half ${esc(money(context.low))}–${esc(money(context.high))}. This comparison does not adjust for this item's condition.</p>
  </div>`;
}

function feedbackPanel(listingId, saved = {}, notice = "") {
  const pending = pendingEmailFeedback?.listing === listingId
    ? pendingEmailFeedback.vote : "";
  const issues = [
    ["wrong_item", "Wrong item"], ["wrong_condition", "Wrong condition"],
    ["wrong_price", "Wrong price"], ["service_or_ad", "Service or ad"],
    ["other", "Something else"]
  ];
  const correctionField = saved.issue === "wrong_item"
    ? `<label>Correct item name<input data-correction-value="corrected_item_type" type="text" maxlength="80" required value="${esc(saved.corrected_item_type || "")}" placeholder="Example: Nike baseball cleats"></label>`
    : saved.issue === "wrong_condition"
      ? `<label>Correct condition<select data-correction-value="corrected_condition" required>
          <option value="">Choose condition</option>
          ${["new", "like_new", "good", "fair", "poor", "needs_repair", "unknown"].map(grade =>
            `<option value="${grade}" ${saved.corrected_condition === grade ? "selected" : ""}>${esc(cleanLabel(grade))}</option>`).join("")}
        </select></label>`
      : saved.issue === "wrong_price"
        ? `<label>Correct seller asking price ($)<input data-correction-value="corrected_price" type="number" min="0" max="1000000" step="0.01" required value="${esc(saved.corrected_price ?? "")}"></label>`
        : "";
  return `<section class="item-feedback" data-feedback-id="${esc(listingId)}" aria-label="Scout feedback">
    <p class="feedback-question">Is Scout's analysis right?</p>
    ${pending ? `<p class="feedback-prompt">From your email: tap ${pending === "up" ? "👍" : "👎"} below to save your feedback.</p>` : ""}
    <div class="feedback-actions">
      <button type="button" class="feedback-button ${pending === "up" ? "suggested" : ""}" data-feedback-vote="up" aria-pressed="${saved.vote === "up"}">👍 Yes</button>
      <button type="button" class="feedback-button ${pending === "down" ? "suggested" : ""}" data-feedback-vote="down" aria-pressed="${saved.vote === "down"}">👎 Needs correction</button>
    </div>
    ${saved.vote === "down" ? `<div class="feedback-issues"><span>What needs correcting?</span>
      ${issues.map(([code, label]) => `<button type="button" class="feedback-issue" data-feedback-issue="${code}" aria-pressed="${saved.issue === code}">${label}</button>`).join("")}
    </div>` : ""}
    ${saved.vote === "down" && saved.issue ? `<form class="feedback-correction" data-correction-form>
      ${correctionField}
      <label>${saved.issue === "other" ? "What was wrong?" : "Extra detail (optional)"}<textarea data-correction-note maxlength="300" ${saved.issue === "other" ? "required" : ""} placeholder="A brief correction helps review this result">${esc(saved.note || "")}</textarea></label>
      <button type="submit">Save correction</button>
      <small>Your correction is saved for review. It does not change other users' results automatically.</small>
    </form>` : ""}
    <p class="feedback-status" data-feedback-status role="status">${esc(notice || (saved.vote ? "Your feedback is saved." : ""))}</p>
  </section>`;
}

function usedCandidateList(result) {
  const rows = result?.candidates || [];
  if (!rows.length) return "<p>No close title matches found in this search. Try the source links above.</p>";
  return `<p>${esc(plural(rows.length, "possible match"))} from Craigslist. Asking prices only; check the exact model, included parts, and condition before comparing. These have not changed Scout's price rating.</p>
    <ul class="used-candidate-list">${rows.map(row => `<li><a href="${esc(row.url)}" target="_blank" rel="noopener noreferrer">${esc(row.title)}</a>
      <span>${esc(money(row.asking_price))}${row.location ? ` · ${esc(row.location)}` : ""}</span></li>`).join("")}</ul>`;
}

function usedResearch(plan, listingId) {
  if (!plan || !Array.isArray(plan.sources) || !plan.sources.length) return "";
  const links = plan.sources.map(source =>
    `<a href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.name)} ↗</a>`).join("");
  const cached = usedCandidatesByItem.get(String(listingId));
  return `<details class="retail-research"><summary>Find more used asking prices</summary>
    <p>Scout has too few comparable asking prices. Check the same model, size, included parts, and condition. Set your area on OfferUp; Mercari prices may include shipping. Search results are leads, not approved comparisons.</p>
    <div class="retail-source-links">${links}</div>
    <button class="used-candidates-button" type="button" data-used-candidates-id="${esc(listingId)}">Check nearby Craigslist prices</button>
    <div class="used-candidate-results" aria-live="polite">${cached ? usedCandidateList(cached) : ""}</div>
    <p>These sites show asking prices. Scout will not change this price bar until a matching listing is reviewed.</p></details>`;
}

function tripCalculator(item) {
  if (item.pet_listing || (item.analysis_state && item.analysis_state !== "complete") ||
      !Number.isFinite(item.asking_price) || item.asking_price < 0 ||
      item.price_source === "unconfirmed" ||
      item.review_flags?.includes("zero_price_card_may_not_be_free") ||
      item.valuation?.price_category_reason === "bundle_asking_price_scope_unclear") return "";
  const valuation = item.valuation || {};
  const hasReference = valuation.status === "provisional" &&
    ["local_asking_range", "broader_area_asking_range"].includes(valuation.price_type) &&
    Number(valuation.comparable_count) >= 3 &&
    Number.isFinite(valuation.midpoint) && valuation.midpoint >= 0 &&
    valuation.price_category?.code && !item.second_opinion?.review_required;
  const referenceMedian = hasReference ? valuation.midpoint : null;
  const suggestedMiles = Number.isFinite(item.distance_miles_approx)
    ? String(Math.round(item.distance_miles_approx * 20) / 10) : "";
  const saved = tripInputsByItem.get(String(item.id)) || {};
  const values = {
    roundTripMiles: saved.roundTripMiles ?? suggestedMiles,
    costPerMile: saved.costPerMile ?? "0.50",
    repairCost: saved.repairCost ?? "0",
    otherCost: saved.otherCost ?? "0",
    budget: saved.budget ?? ""
  };
  const input = (label, field, step, max) => `<label>${label}<input type="number" inputmode="decimal"
    data-trip-field="${field}" min="0" max="${max}" step="${step}" value="${esc(values[field])}"></label>`;
  return `<details class="trip-check" data-trip-id="${esc(item.id)}"
    data-asking-price="${esc(item.asking_price)}" data-reference-median="${referenceMedian === null ? "" : esc(referenceMedian)}">
    <summary>Worth the trip? Estimate your total cost</summary>
    <div class="trip-check-body">
      <p>Seller asks <strong>${esc(money(item.asking_price))}</strong>. Enter your own travel, repair and pickup costs.</p>
      <div class="trip-fields">
        ${input("Round-trip driving miles", "roundTripMiles", "0.1", "10000")}
        ${input("Travel cost per mile ($)", "costPerMile", "0.01", "1000000")}
        ${input("Repairs or missing parts ($)", "repairCost", "0.01", "1000000")}
        ${input("Other pickup costs ($)", "otherCost", "0.01", "1000000")}
        ${input("Your max total budget ($, optional)", "budget", "0.01", "1000000")}
      </div>
      <p class="trip-assumption">Miles start at twice Scout’s approximate seller-town distance. Replace this with your driving distance. The $0.50/mile rate is an example you can change; $0 repairs is unverified.</p>
      <p class="trip-result" role="status"></p>
      <p class="trip-comparison"></p>
      <p class="trip-caution">${referenceMedian === null
        ? "Scout will add a comparison when it has enough matching listings."
        : `Comparison uses ${esc(valuation.comparable_count)} similar asking prices ${esc(referenceScope(valuation).detail)} observed within the last year.`} Asking prices are not completed sales. Check the seller’s price, condition and pickup details before traveling.</p>
    </div>
  </details>`;
}

function itemCard(item) {
  const pet = Boolean(item.pet_listing);
  const analysisPending = item.analysis_state && item.analysis_state !== "complete";
  const rawTitle = item.display_title || (item.asking_price === 0
    ? item.title.replace(/^free\b[\s:!.,;\-–—]*/i, "").replace(/[\s!.,;:\-–—]+$/g, "") || item.title
    : item.title);
  const title = rawTitle[0].toUpperCase() + rawTitle.slice(1);
  const grade = pet ? "" : item.condition_grade || "unknown";
  const usage = pet ? "" : item.usage_state || "unknown";
  const gradeStyle = ["needs_repair", "poor"].includes(grade) ? "red" : grade === "unknown" ? "amber" : "teal";
  const summary = item.generated_description || item.refactored_description || item.description ||
    "No seller details were available.";
  const evidence = item.condition_evidence?.length
    ? `<ul>${item.condition_evidence.map(point => `<li>${esc(point)}</li>`).join("")}</ul>`
    : "<p>No condition evidence was captured.</p>";
  const flags = item.review_flags?.length
    ? `<ul>${item.review_flags.map(flag => `<li>${esc(flagLabels[flag] || cleanLabel(flag))}</li>`).join("")}</ul>`
    : "<p>Review the photo and seller details before relying on this result.</p>";
  const link = sourceUrl(item.url);
  const photoUrls = (Array.isArray(item.image_urls) && item.image_urls.length
    ? item.image_urls : [item.image_url]).slice(0, 4).map(appPath);
  const distance = typeof item.distance_miles_approx === "number"
    ? `≈ ${item.distance_miles_approx.toFixed(1)} mi away`
    : "Distance unavailable";
  const priceLabel = ["seller_description", "seller_title"].includes(item.price_source)
    ? `Seller asks ${money(item.asking_price)}`
    : item.price_source === "unconfirmed" ? "Price unclear" : money(item.asking_price);
  const scope = referenceScope(item.valuation);
  const bundle = Array.isArray(item.valuation?.bundle_components);
  const itemized = item.valuation?.price_type === "itemized_asking_range";
  const localValue = item.valuation?.status === "provisional" &&
    ["local_asking_range", "itemized_asking_range"].includes(item.valuation?.price_type) &&
    typeof item.valuation.low === "number" && typeof item.valuation.high === "number"
    ? `${itemized ? "Combined individual" : scope.label} asking reference ${money(item.valuation.low)}–${money(item.valuation.high)}` : "";
  const couch = item.couch_quality;
  const second = item.second_opinion;
  const priceGuideNotes = {
    couch_quality_requires_review: "Price rating paused: couch condition needs review.",
    couch_quality_poor_no_cohort: "Price rating paused: no comparable poor-condition couch group yet.",
    insufficient_condition_comparables: "Price rating paused: fewer than three local couches match this condition.",
    second_opinion_item_type_conflict: "Price rating paused: the two local models disagree about the item type.",
    second_opinion_condition_conflict: "Price rating paused: the two local models disagree about condition.",
    bundle_asking_price_scope_unclear: "Price rating paused: the seller offers pieces separately, so the listed price may not cover the whole bundle."
  };
  const priceGuideNote = priceGuideNotes[item.valuation?.price_category_reason];
  const context = item.valuation?.asking_context;
  const askingContext = context && Number.isFinite(context.midpoint)
    ? `Median ask ${money(context.midpoint)} from ${context.comparable_count} ${context.basis} listings ${scope.detail}. This does not adjust for this item's condition.`
    : "";
  const shortage = ["insufficient_local_product_comparables",
    "insufficient_local_furniture_comparables",
    "insufficient_verified_comparables", "insufficient_same_variant_comparables",
    "mower_variant_details_missing"].includes(item.valuation?.reason);
  const comparableCount = Number(item.valuation?.comparable_count || 0);
  const missingParts = item.valuation?.missing_components || [];
  const incompleteNote = item.valuation?.reason === "missing_major_components"
    ? `Price rating paused: seller reports missing ${missingParts.join(", ")}. Prices for complete items do not apply.`
    : item.valuation?.reason === "parts_only_listing"
      ? "Price rating paused: this is a parts-only item. Prices for complete items do not apply."
      : "";
  const cohort = item.valuation?.price_cohort;
  const cohortLabel = cohort?.attributes?.power_source === "battery"
    ? `${cohort.attributes.brand ? cohort.attributes.brand.toUpperCase() + " " : ""}${cohort.attributes.voltage || "?"}V ${cleanLabel(cohort.key)}${cohort.attributes.deck_inches ? `, ${cohort.attributes.deck_inches}-inch` : ""}${cohort.attributes.kit === "battery_and_charger" ? " with battery and charger" : cohort.attributes.kit === "tool_only" ? " without battery" : ""}`
    : cohort?.label;
  const bundleGap = item.valuation?.reason === "bundle_quantities_unclear"
    ? "Bundle price pending: the seller has not clearly confirmed how many of each item are included."
    : item.valuation?.reason === "insufficient_bundle_component_comparables"
      ? `Bundle price pending: ${item.valuation.bundle_components.map(part => `${part.label}: ${part.comparable_count || 0}/3 single-item prices`).join(" · ")}.`
      : "";
  const comparisonGap = incompleteNote || bundleGap || (pet
    ? "Price comparison is not offered for pets."
    : item.valuation?.reason === "mixed_item_lot"
    ? "Price rating paused: this listing offers different items, so one comparison would be misleading."
    : item.valuation?.reason === "mower_power_source_unclear"
    ? "Price rating pending: the mower's power source is unclear. Scout will keep it separate from gas and battery mower prices."
    : item.valuation?.reason === "scooter_variant_details_missing"
    ? "Price rating pending: the scooter model is not confirmed. Scout will not compare children's scooters with higher-end models."
    : shortage
    ? `Price rating pending: ${comparableCount} of ${item.valuation?.needed || 3} comparable ${cohortLabel || "local"} asking prices found ${scope.detail}.${cohort?.parent === "personal_transport/electric_scooter" ? " Standing scooters are excluded." : cohort ? " Other mower types are excluded." : ""}${comparisonRequirements(item.valuation) ? ` Required match: ${comparisonRequirements(item.valuation)}.` : ""}`
    : item.valuation?.reason === "price_group_unavailable"
      ? "Price comparison unavailable: Scout identified the item but has no reliable comparison group for it yet."
    : item.valuation?.reason === "item_identity_unconfirmed"
      ? item.item_type && !["unknown", "item", "mixed_or_unclear"].includes(item.item_type)
        ? "Price comparison unavailable: Scout knows the item type but has no reliable comparison group for it yet."
        : "Price comparison unavailable: this listing's item type is unclear."
      : "");
  return `<article class="panel item-card" id="item-${esc(item.id)}">
    <a class="item-image-link" href="${esc(photoUrls[0])}" target="_blank" rel="noopener noreferrer" aria-label="Open full photo for ${esc(title)}"><img class="item-image" src="${esc(photoUrls[0])}" alt="Saved photo for ${esc(title)}" loading="lazy"></a>
    ${photoUrls.length > 1 ? `<div class="item-photo-strip" aria-label="${esc(photoUrls.length)} seller photos">${photoUrls.map((url, index) => `<button class="item-photo-thumb" type="button" data-photo-url="${esc(url)}" aria-label="Show photo ${index + 1} of ${photoUrls.length}" aria-pressed="${index === 0 ? "true" : "false"}"><img src="${esc(url)}" alt="" loading="lazy"></button>`).join("")}</div><p class="item-photo-caption">${esc(photoUrls.length)} listing photos · select one to view it</p>` : ""}
    <div class="item-body">
      <div class="item-kicker"><span>${esc(distance)}</span><span class="item-price">${esc(priceLabel)}</span></div>
      <h3>${esc(title)}</h3>
      ${analysisPending ? `<p class="item-price-note" role="status">${item.analysis_state === "incomplete" ? "Analysis stopped before condition and price checks finished. Treat this identification as unconfirmed." : "Early item identification. Scout is still checking condition and comparable prices; only verified matches will trigger email."}</p>` : ""}
      <p class="item-summary">${esc(summary.length > 170 ? summary.slice(0, 167) + "…" : summary)}</p>
      ${localValue ? `<p class="item-value">${esc(localValue)}</p>` : ""}
      ${!analysisPending && localValue && comparisonRequirements(item.valuation)
        ? `<p class="item-cohort">Compared with: ${esc(comparisonRequirements(item.valuation))}</p>` : ""}
      ${bundle ? bundleBreakdown(item.valuation) : ""}
      ${cohort && (cohort.attributes?.form === "seated" ||
        (cohort.attributes?.power_source && !["unknown", "conflict", "electric_unknown"].includes(cohort.attributes.power_source)))
        ? `<p class="item-cohort">${item.valuation?.status === "unvalued" ? "Looking for" : "Compared as"}: ${esc(cohortLabel)}</p>` : ""}
      ${analysisPending ? "" : askingContextGuide(item)}
      ${analysisPending ? "" : priceGuide(item)}
      ${!analysisPending && !bundle ? comparableEvidence(item.valuation) : ""}
      ${priceGuideNote ? `<p class="item-price-note">${esc(priceGuideNote)}</p>` : ""}
      ${!analysisPending && comparisonGap ? `<p class="item-price-note${incompleteNote ? " incomplete" : ""}">${esc(comparisonGap)}</p>` : ""}
      ${usedResearch(item.used_research, item.id)}
      ${tripCalculator(item)}
      <div class="item-meta">
        <span class="chip">Item: ${esc(cleanLabel(item.item_type || "unclear"))}</span>
        ${analysisPending ? '<span class="chip amber">Early result</span>' : ""}
        ${bundle ? '<span class="chip">Bundle</span>' : ""}
        ${pet ? '<span class="chip teal">Pet listing</span>' : analysisPending ? '<span class="chip amber">Condition check pending</span>' : `<span class="chip teal">New or used: ${esc(usage)}</span><span class="chip ${gradeStyle}">Condition: ${esc(cleanLabel(grade))}</span>`}
        ${second?.review_required ? '<span class="chip amber">AI review needed</span>' : ""}
        ${incompleteNote ? '<span class="chip red">Incomplete item</span>' : ""}
        ${item.review_flags?.includes("zero_price_card_may_not_be_free")
          ? '<span class="chip red">Price conflict</span>' : ""}
      </div>
      ${analysisPending
        ? `<p class="analysis-confidence">Early item confidence: ${esc(item.item_confidence || "low")} · condition and price checks are pending</p>`
        : `<p class="analysis-confidence">AI confidence · item: ${esc(item.item_confidence || "low")}${pet ? "" : ` · condition: ${esc(item.condition_confidence || "low")}`} · price: ${esc(item.price_confidence || "unrated")}</p>`}
      ${!analysisPending && grade === "unknown" ? `<p class="condition-explainer">${esc(item.condition_reason || "Scout has not assigned a reliable wear grade.")}</p>` : ""}
      ${feedbackEnabled && !analysisPending ? feedbackPanel(item.id, feedbackByItem[item.id]) : ""}
      <details>
        <summary>See AI result and seller details</summary>
        <div class="detail-content">
          <dl>
            <dt>AI item category</dt><dd>${esc(item.category || "Unknown")}</dd>
            <dt>Image model</dt><dd>${esc(item.vision_model || "Unavailable")}</dd>
            ${pet ? "" : `<dt>Condition model</dt><dd>${esc(item.condition_model || "Unavailable")}</dd>`}
            ${item.price_source !== "facebook_card" ? `<dt>Facebook card price</dt><dd>${esc(money(item.card_asking_price))}</dd>` : ""}
            ${["seller_description", "seller_title"].includes(item.price_source) ? `<dt>Seller asking price</dt><dd>${esc(money(item.seller_asking_price))} in ${item.price_source === "seller_title" ? "title" : "description"}</dd>` : ""}
            ${typeof item.previous_price === "number" ? `<dt>Other price shown on card</dt><dd>${esc(money(item.previous_price))}; the Facebook card shows ${esc(money(item.card_asking_price ?? item.asking_price))}</dd>` : ""}
            <dt>Suggested identity</dt><dd>${esc(item.identity || "No specific model confirmed")}</dd>
            ${pet ? "" : `<dt>Condition estimate</dt><dd>${esc(cleanLabel(grade))} · ${esc(item.condition_confidence || "unrated")} confidence</dd>`}
            ${!pet && second ? `<dt>Local second opinion</dt><dd>${esc(second.model || "Local model")}: ${esc(cleanLabel(second.result?.item_type || "item unclear"))}; ${esc(cleanLabel(second.result?.condition_grade || "unknown"))} condition · ${esc(second.result?.condition_confidence || "low")} confidence.${second.review_required ? " The models disagree; review recommended." : ""}</dd>
              <dt>Why reviewed</dt><dd>${esc((second.reasons || []).map(cleanLabel).join("; "))}</dd>
              <dt>Second opinion evidence</dt><dd>${esc((second.result?.evidence || []).join("; ") || "No specific evidence supplied")}</dd>` : ""}
            ${couch ? `<dt>Couch quality check</dt><dd>8B model: ${esc(cleanLabel(couch.model_grade))}; guarded: ${esc(cleanLabel(couch.guarded_grade))} · ${esc(couch.guarded_confidence)} confidence.${couch.review_required ? " Review recommended." : ""}</dd>
              <dt>Photo source</dt><dd>${esc(cleanLabel(couch.photo_kind))}</dd>
              <dt>Color clue</dt><dd>${esc(couch.color_for_matching || "unknown")}${couch.model_color !== couch.color_for_matching ? ` (model saw ${esc(couch.model_color || "unknown")})` : ""}</dd>
              <dt>Upholstery</dt><dd>${esc(couch.upholstery || "unknown")}</dd>
              <dt>Seller-reported wear</dt><dd>${esc((couch.seller_reported_issues || []).join("; ") || "None specified")}</dd>
              <dt>Visible wear</dt><dd>${esc((couch.visible_issues || []).join("; ") || "None confirmed")}</dd>
              <dt>Quality rules</dt><dd>${esc((couch.reasons || []).join(" ") || "No grade change needed")}</dd>` : ""}
            ${pet ? "" : `<dt>New or used</dt><dd>${esc(cleanLabel(usage))}</dd><dt>Function</dt><dd>${esc(cleanLabel(item.functional_status))}</dd>`}
            <dt>Generated item description</dt><dd>${esc(item.generated_description || "Unavailable")}</dd>
            <dt>Original seller title</dt><dd>${esc(item.title)}</dd>
            <dt>Seller description</dt><dd>${esc(item.description || "Unavailable")}</dd>
            <dt>Cleaned description</dt><dd>${esc(item.refactored_description || "No item details selected")}</dd>
            ${pet ? "" : `<dt>Condition evidence</dt><dd>${evidence}</dd>`}
            <dt>Review notes</dt><dd>${flags}</dd>
            <dt>Value estimate</dt><dd>${localValue
              ? itemized
                ? `${esc(localValue)}. Sum of separate item medians: ${esc(money(item.valuation.midpoint))}. These are individual asking prices, not bundle sale prices.`
                : `${esc(localValue)}. Median ask ${esc(money(item.valuation.midpoint))} from ${esc(item.valuation.comparable_count)} similar listings. These are asking prices, not completed sales.`
              : askingContext ? esc(askingContext)
              : item.valuation_status === "estimated" ? "Estimated from matched asking-price listings; details require review."
              : esc(comparisonGap || "Not estimated")}</dd>
            ${item.valuation?.price_category_reason === "card_and_description_price_conflict" ? '<dt>Price guide</dt><dd>Unavailable because the card and seller description show different prices.</dd>' : ""}
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
      (!grade || item.pet_listing || (item.condition_grade || "unknown") === grade);
  });
  if (sort === "distance") selected = selected.slice().sort((a, b) =>
    (a.distance_miles_approx ?? Infinity) - (b.distance_miles_approx ?? Infinity));
  if (sort === "title") selected = selected.slice().sort((a, b) =>
    (a.display_title || a.title).localeCompare(b.display_title || b.title));
  document.getElementById("visible-count").textContent = plural(selected.length, "item");
  const grid = document.getElementById("results-grid");
  grid.classList.toggle("single-result", selected.length === 1);
  grid.innerHTML = selected.length
    ? selected.map(itemCard).join("")
    : `<div class="panel empty">${esc(items.length ? "No items match these page filters." : emptyMessage)}</div>`;
  applyBarPositions(grid);
  grid.querySelectorAll(".trip-check").forEach(panel => updateTripCalculator(panel));
}

async function showScan(id) {
  try {
    if (!publicReadOnly && !publicHost && !selectedEmail()) {
      location.href = appPath("/");
      return;
    }
    const [scan, history, matches, account, feedbackState] = await Promise.all([
      api(`/api/scans/${encodeURIComponent(id)}`),
      publicReadOnly ? api("/api/scans") : accountApi("/api/scans"),
      publicReadOnly ? Promise.resolve({unread_count: 0}) : accountApi("/api/alerts"),
      publicReadOnly ? Promise.resolve(null) : accountApi("/api/account"),
      publicReadOnly ? Promise.resolve({feedback: {}}) :
        accountApi(`/api/scans/${encodeURIComponent(id)}/feedback`)
    ]);
    activeAccount = account;
    feedbackScanId = id;
    feedbackByItem = feedbackState.feedback || {};
    feedbackEnabled = !publicReadOnly && Boolean(account?.configured);
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
    const newOnlyScan = scan.collection_new_only || scan.mode === "incremental";
    const noMatchNotice = scan.status === "complete" && !scan.result_count
      ? newOnlyScan && !scan.scanned_count
        ? `No new listings among ${esc(plural(scan.cards_checked || 0, "visible card"))} checked. ${esc(scan.known_cards_skipped || 0)} were already reviewed for this search.`
      : priceExcluded.length === scan.nearby_count && scan.nearby_count > 0
        ? `No results matched your $${esc(scan.watch_item.min_price)}–$${esc(scan.watch_item.max_price)} price range. All ${esc(plural(scan.nearby_count, "nearby listing"))} were outside that range. Open Ads filtered to see their prices.`
        : !scan.nearby_count
          ? `None of the ${esc(plural(scan.scanned_count || 0, "scanned card"))} were within the ${esc(scan.radius_miles)}-mile radius. Facebook also showed out-of-area cards; open Cards scanned to review them. A wider radius may find more items.`
          : `No listings passed every filter in this scan. Open Ads filtered to see why the ${esc(plural(scan.filtered_out_count || 0, "nearby listing"))} were excluded.`
      : "";
    app.innerHTML = `
      <a class="back-link" href="${appPath("/")}">← All scans</a>
      <section class="scan-header">
        <div><p class="eyebrow">Saved scan · ${esc(prettyDate(scan.created_at))}</p>
          <h1>${esc(scan.zip_code)} within ${esc(scan.radius_miles)} miles</h1>
          <p class="lead">Keywords: ${esc((scan.keywords || []).join(", "))}.${scan.watch_item ? ` Listed price: $${esc(scan.watch_item.min_price)}–$${esc(scan.watch_item.max_price)}.` : ""} ${newOnlyScan ? `${scan.mode === "incremental" ? "Fast" : "Daily"} check: ${esc(scan.cards_checked || 0)} visible cards checked, ${esc(scan.known_cards_skipped || 0)} unchanged cards skipped. New or edited cards get full analysis.` : "Only physical items are shown."} Locations use displayed seller towns, so distances are approximate.</p>
        </div>
        ${statusTag(scan.status)}
      </section>
      <div class="stats">
        <button class="panel stat stat-button" type="button" data-quick-list="scanned" aria-controls="quick-list-panel" aria-expanded="false"><span class="stat-label">${newOnlyScan ? "Cards reviewed · view list" : "Cards scanned · view list"}</span><strong class="stat-value">${esc(scan.scanned_count || 0)}</strong></button>
        <div class="panel stat"><span class="stat-label">Nearby cards</span><strong class="stat-value">${esc(scan.nearby_count || 0)}</strong></div>
        <div class="panel stat"><span class="stat-label">Items shown</span><strong class="stat-value">${esc(scan.result_count || 0)}</strong></div>
        <button class="panel stat stat-button" type="button" data-quick-list="filtered" aria-controls="quick-list-panel" aria-expanded="false"><span class="stat-label">Ads filtered · view list</span><strong class="stat-value">${esc(scan.filtered_out_count || 0)}</strong></button>
      </div>
      ${Number(scan.search_center_offset_miles) >= 1 &&
        Number(scan.search_coverage_radius_miles) > Number(scan.radius_miles)
          ? `<p class="scan-notice">Facebook centered this search about ${esc(scan.search_center_offset_miles)} miles from your ZIP. Scout searched ${esc(scan.search_coverage_radius_miles)} miles around that center, then kept listings estimated within your ${esc(scan.radius_miles)}-mile ZIP radius.</p>`
          : ""}
      ${scan.photo_analysis ? `<details class="panel scan-review"><summary>How Scout reviewed these cards</summary>
        <ul><li>${esc(scan.photo_analysis.analyzed || 0)} checked with photos and seller text</li>
          <li>${esc(scan.photo_analysis.seller_offers_filtered || 0)} services, promotions, or events screened out from seller text</li>
          <li>${esc(scan.photo_analysis.outside_price_range || 0)} outside your saved price range</li>
          <li>${esc((scan.photo_analysis.errors || 0) + (scan.photo_analysis.photos_missing || 0))} could not be analyzed yet</li></ul>
        <p>Unfinished items remain eligible for a later scan; Scout does not count them as confirmed matches.</p>
      </details>` : ""}
      ${scan.rechecked_cards ? `<p class="scan-notice">${esc(plural(scan.rechecked_cards, "previously seen card"))} had a changed title or price, or needed a one-time refresh after Scout's tracking upgrade.</p>` : ""}
      ${scan.new_cards_deferred ? `<p class="scan-notice">${esc(plural(scan.new_cards_deferred, "additional new or edited card"))} were found beyond this scan's review limit. They remain eligible for the next check.</p>` : ""}
      ${coverageProbePanel(scan.coverage_probe)}
      ${queueSection(queueForDisplay(history, account))}
      ${noMatchNotice ? `<p class="scan-notice">${noMatchNotice}</p>` : ""}
      ${scan.nearby_count === 0 && scan.closest_outside?.length ? `<section class="panel outside-panel">
        <h2>Closest cards outside your radius</h2>
        <p>These were visible on Facebook but were not analyzed as matches for your ${esc(scan.radius_miles)}-mile search. Distances use seller towns, not exact addresses.</p>
        <ol class="quick-list">${scan.closest_outside.map(outsideCardRow).join("")}</ol>
        <a class="open-link" href="${appPath("/setup")}">Adjust your search radius →</a>
      </section>` : ""}
      ${scan.unresolved_location_count ? `<p class="scan-notice">${esc(plural(scan.unresolved_location_count, "card"))} had no usable seller town, so Scout could not confirm whether they were within your radius.</p>` : ""}
      <section class="panel quick-panel" id="quick-list-panel" hidden aria-live="polite"></section>
      ${scan.analysis_retry_pending_count
        ? `<p class="scan-notice" role="status">Analysis did not finish for ${esc(plural(scan.analysis_retry_pending_count, "nearby listing"))}. If still available, ${scan.analysis_retry_pending_count === 1 ? "it stays" : "they stay"} eligible for your next scan. Review the item cards or Ads filtered list for details.</p>`
        : scan.ai_error_count
          ? `<p class="scan-notice">AI photo analysis was incomplete for ${esc(plural(scan.ai_error_count, "nearby card"))}. Open Ads filtered to review the listing details.</p>`
          : ""}
      ${running ? `<section class="panel progress-panel" role="status"><h2>${esc(scan.stage)}</h2>
        <p>${scan.stage_progress
          ? `${esc(scan.stage_progress.label)}: ${esc(scan.stage_progress.processed)} of ${esc(scan.stage_progress.total)} checked. `
          : ""}${scan.pending_analysis_count ? `${esc(plural(scan.pending_analysis_count, "nearby card"))} still waiting for photo identification. ` : ""}Early item results appear here as the scan progresses. This page refreshes automatically.</p>
        <div class="progress-track" aria-hidden="true"><span${scan.stage_progress
          ? ` class="measured" data-bar-width="${Math.round(100 * scan.stage_progress.processed / scan.stage_progress.total)}"`
          : ""}></span></div></section>` : ""}
      ${scan.status === "failed" || scan.status === "interrupted"
        ? `<section class="panel progress-panel" role="alert"><h2>${esc(scan.stage)}</h2><p>${esc(scan.error || "Start a new scan to try again.")}</p>
          ${scan.recovered_by_scan_id ? `<p><a class="open-link" href="${appPath(`/scan/${encodeURIComponent(scan.recovered_by_scan_id)}`)}">Open completed retry →</a></p>` : ""}</section>`
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
              <option value="good">Good</option><option value="fair">Fair</option><option value="poor">Poor</option>
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
    applyBarPositions(app);
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
        const heading = scanned ? (newOnlyScan
          ? "New or edited cards reviewed in this scan" : "Cards collected in this scan")
          : "Nearby cards left out of results";
        panel.innerHTML = `<div class="quick-heading"><h2>${heading}</h2><span>${esc(plural(cards.length, "card"))}</span></div>
          ${cards.length ? `<ol class="quick-list">${cards.map(quickCardRow).join("")}</ol>`
            : '<p class="form-help">No card details were saved for this list.</p>'}`;
      });
    });
    renderResults(scan.listings, scan.watch_item
      ? `No confirmed items within the saved $${scan.watch_item.min_price}–$${scan.watch_item.max_price} price range. Open Ads filtered to see why nearby cards were excluded.`
      : "No confirmed physical items in this scan. Open Ads filtered to review exclusions.");
    const reviewTarget = location.hash.slice(1) ||
      (pendingEmailFeedback?.listing ? `item-${pendingEmailFeedback.listing}` : "");
    if (reviewTarget) document.getElementById(reviewTarget)?.scrollIntoView();
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
    updateActivitySection(history.activity);
    updateCoveragePilotSection(history);
    fillPicker(history.scans, id);
    if (queueForDisplay(history, activeAccount).length)
      pollTimer = setTimeout(() => pollScanQueue(id), 4500);
  } catch { /* The next page load can retry if Scout is temporarily unavailable. */ }
}

function couchPriceSummary(summary) {
  return ["sofa", "loveseat", "sectional"].map(type => {
    const used = summary.by_subtype?.[type]?.used_only || {};
    return `<div class="panel comp-summary"><span>${esc(type)}</span>
      <strong>${used.count ? esc(money(used.median_asking_price)) : "No sample"}</strong>
      <small>${esc(used.count || 0)} used asking prices${used.middle_half_asking_range
        ? ` · middle half ${esc(money(used.middle_half_asking_range[0]))}–${esc(money(used.middle_half_asking_range[1]))}` : ""}</small></div>`;
  }).join("");
}

function couchQualityPilot(assessment, guardrails) {
  if (!assessment) return "";
  const photoKind = assessment.photo_kind === "catalog_or_graphic"
    ? "Catalog-style image" : assessment.photo_kind === "seller_photo"
      ? "Likely seller photo" : "Photo source unclear";
  const issues = assessment.seller_reported_issues || [];
  const visible = assessment.visible_issues || [];
  const cautions = [];
  if (assessment.photo_kind === "catalog_or_graphic")
    cautions.push("This photo cannot verify the actual couch's condition or color.");
  if (issues.length && ["new", "like_new", "good"].includes(assessment.quality_grade))
    cautions.push("The model's grade may be optimistic given the seller-reported issues.");
  if (assessment.confidence === "high" && !(assessment.photo_evidence || []).length &&
      !(assessment.seller_evidence || []).length)
    cautions.push("High confidence without specific supporting evidence.");
  return `<section class="quality-pilot" aria-label="Eight billion parameter model quality pilot">
    <h4>8B couch quality pilot</h4>
    <p><strong>Model suggests:</strong> ${esc(cleanLabel(assessment.quality_grade))} · ${esc(assessment.confidence)} confidence</p>
    ${guardrails ? `<p class="quality-guarded"><strong>Guarded quality:</strong> ${esc(cleanLabel(guardrails.guarded_grade))} · ${esc(guardrails.guarded_confidence)} confidence${guardrails.review_required ? " · review needed" : ""}</p>
      ${(guardrails.reasons || []).length ? `<ul class="quality-reasons">${guardrails.reasons.map(reason => `<li>${esc(reason)}</li>`).join("")}</ul>` : ""}` : ""}
    <p><strong>Photo:</strong> ${esc(photoKind)} · <strong>Model color:</strong> ${esc(assessment.color || "unknown")} · <strong>Color clue:</strong> ${esc(guardrails?.color_for_matching || assessment.color || "unknown")} · <strong>Upholstery:</strong> ${esc(assessment.upholstery || "unknown")}</p>
    <p><strong>Visible issues:</strong> ${esc(visible.length ? visible.join("; ") : "None clearly identified")}</p>
    <p><strong>Seller-reported issues:</strong> ${esc(issues.length ? issues.join("; ") : "None stated")}</p>
    <details><summary>Model evidence and uncertainty</summary>
      <p><strong>Photo evidence:</strong> ${esc((assessment.photo_evidence || []).join("; ") || "None stated")}</p>
      <p><strong>Seller evidence:</strong> ${esc((assessment.seller_evidence || []).join("; ") || "None stated")}</p>
      <p><strong>Uncertainty:</strong> ${esc((assessment.uncertainties || []).join("; ") || "None stated")}</p>
    </details>
    ${cautions.length ? `<ul class="quality-cautions">${cautions.map(note => `<li>${esc(note)}</li>`).join("")}</ul>` : ""}
    <small>Pilot only. This grade does not change the price guide.</small>
  </section>`;
}

function couchCard(item) {
  const link = sourceUrl(item.url);
  const ask = item.price_source === "seller_description" ? "Seller asks in description" : "Listed price";
  const reviewLabels = {
    item_type_unclear: "Item type is unclear",
    ambiguous_loveseat_or_sofa: "Could be a loveseat or sofa",
    bundle_or_multiple_items: "May include multiple items",
    zero_card_seller_asks: "Card says free, seller asks for money",
    free_or_placeholder_price: "Free or placeholder price",
    card_and_description_price_conflict: "Card and description prices differ",
    title_and_condition_conflict: "Condition details conflict",
    description_unavailable: "Seller description was unavailable"
  };
  const reasons = item.review_reasons ? item.review_reasons.split(", ").map(reason =>
    reviewLabels[reason] || cleanLabel(reason)).join(" · ") : "";
  return `<article class="panel comp-card">
    <img src="${esc(appPath(item.image_url))}" alt="Saved Marketplace photo for ${esc(item.title)}" loading="lazy">
    <div class="comp-body"><div class="comp-top"><span>${esc(item.location || "Location unknown")}${typeof item.distance_miles_approx === "number" ? ` · ≈ ${esc(item.distance_miles_approx.toFixed(1))} mi` : ""}</span><strong>${esc(money(item.effective_ask))}</strong></div>
      <h3>${esc(item.title || "Untitled couch listing")}</h3>
      <p class="comp-meta">${esc(ask)}${item.price_source === "seller_description" ? ` · card says ${esc(money(item.card_price))}` : ""}</p>
      <div class="item-meta"><span class="chip">${esc(item.subtype)}</span>
        ${item.price_reviewed ? '<span class="chip teal">Price reviewed</span>' : ""}
        <span class="chip ${item.in_estimation_pool ? "teal" : "amber"}">${item.in_estimation_pool ? "Used in estimates" : "Outside current estimate pool"}</span>
        <span class="chip">${esc(item.seller_condition_label || cleanLabel(item.condition))}</span></div>
      ${reasons ? `<p class="comp-review">${esc(reasons)}</p>` : ""}
      ${item.price_category ? priceGuide({valuation: {price_category: item.price_category}}) : ""}
      ${couchQualityPilot(item.quality_pilot, item.quality_guardrails)}
      <p class="comp-description">${esc(item.description || "Seller description unavailable.")}</p>
      ${link ? `<a class="open-link" href="${esc(link)}" target="_blank" rel="noopener noreferrer">Open Facebook listing ↗</a>` : ""}
    </div></article>`;
}

async function showCouchComparables() {
  try {
    const [data, account] = await Promise.all([
      api("/api/comparables/couches"), publicHost ? api("/api/account") : Promise.resolve(null)
    ]);
    showAdminLink(account);
    const rows = data.listings || [];
    app.innerHTML = `<a class="back-link" href="${appPath("/admin")}">← Admin</a>
      <section class="comp-hero"><p class="eyebrow">Marketplace price review · ${esc(prettyDate(data.summary.observed_at))}</p>
        <h1>Couch prices near 21009</h1>
        <p class="lead">${esc(rows.length)} listings within approximately 30 miles. ${esc(data.summary.price_reviewed_count || 0)} asking prices reviewed; ${esc(data.summary.estimation_pool_count || 0)} clear single-item listings are used for estimates. These are not completed sales. Distance is estimated from the seller's town.</p></section>
      ${data.summary.current_price_reference ? "" : '<p class="scan-notice">These prices are archived. Scout does not use observations older than one year in current comparisons.</p>'}
      <div class="comp-summary-grid">${couchPriceSummary(data.summary)}</div>
      <div class="comp-controls"><label>Show <select id="comp-status"><option value="all">All ${esc(rows.length)} listings</option><option value="candidate">${esc(data.summary.candidate_comparables)} candidate comps</option><option value="review">${esc(data.summary.needs_review)} with item review notes</option><option value="quality">${esc(data.summary.quality_pilot_count || 0)} in the 8B quality pilot</option></select></label>
        <label>Type <select id="comp-type"><option value="all">All types</option><option value="sofa">Sofas</option><option value="loveseat">Loveseats</option><option value="sectional">Sectionals</option></select></label></div>
      <p id="comp-count" class="comp-count"></p><div id="comp-grid" class="results-grid"></div>
      <p class="caution">Condition and item type come from seller text and need human review. Listing age and final sale prices are unknown.</p>`;
    const render = () => {
      const status = document.getElementById("comp-status").value;
      const type = document.getElementById("comp-type").value;
      const visible = rows.filter(row => (status === "all" ||
        (status === "quality" ? Boolean(row.quality_pilot) : row.review_status === status)) &&
        (type === "all" || row.subtype === type));
      document.getElementById("comp-count").textContent = plural(visible.length, "listing");
      document.getElementById("comp-grid").innerHTML = visible.length
        ? visible.map(couchCard).join("") : '<div class="panel empty">No listings match this filter.</div>';
      applyBarPositions(document.getElementById("comp-grid"));
    };
    document.getElementById("comp-status").addEventListener("change", render);
    document.getElementById("comp-type").addEventListener("change", render);
    const requestedStatus = new URLSearchParams(location.search).get("show");
    if (["candidate", "review", "quality"].includes(requestedStatus))
      document.getElementById("comp-status").value = requestedStatus;
    render();
  } catch (failure) {
    app.innerHTML = `<div class="panel empty">Could not load couch prices: ${esc(failure.message)}</div>`;
  }
}

function generalReferenceCard(item) {
  const link = sourceUrl(item.url);
  const reason = item.review_reasons ? item.review_reasons.split(", ").map(cleanLabel).join(" · ") : "";
  return `<article class="panel comp-card">
    <img src="${esc(appPath(item.image_url))}" alt="Saved Marketplace photo for ${esc(item.title)}" loading="lazy">
    <div class="comp-body"><div class="comp-top"><span>${esc(item.location || "Location unknown")}${item.search_zip ? ` · ZIP ${esc(item.search_zip)}` : ""}</span><strong>${esc(money(item.effective_ask))}</strong></div>
      <h3>${esc(item.title || "Untitled listing")}</h3>
      <div class="item-meta"><span class="chip">${esc(cleanLabel(item.reference_type))}</span>
        <span class="chip ${item.in_estimate_pool || item.review_status === "candidate" ? "teal" : "amber"}">${item.in_estimate_pool ? "Used for estimates" : item.review_status === "candidate" ? "Clear candidate" : "Needs review"}</span>
        <span class="chip">${esc(cleanLabel(item.condition))}</span></div>
      ${reason ? `<p class="comp-review">${esc(reason)}</p>` : ""}
      <p class="comp-description">${esc(item.description || "Seller description unavailable.")}</p>
      ${link ? `<a class="open-link" href="${esc(link)}" target="_blank" rel="noopener noreferrer">Open Facebook listing ↗</a>` : ""}
    </div></article>`;
}

async function showGeneralComparables() {
  try {
    const [data, account] = await Promise.all([
      api("/api/comparables/general"), publicHost ? api("/api/account") : Promise.resolve(null)
    ]);
    showAdminLink(account);
    const rows = data.listings || [];
    const types = Object.entries(data.summary.by_type || {}).sort((a, b) => b[1].candidate_count - a[1].candidate_count);
    const mowerCoverage = data.summary.mower_coverage;
    const mowerCategories = [...(mowerCoverage?.categories || [])].sort((a, b) =>
      a.count - b.count || a.label.localeCompare(b.label));
    app.innerHTML = `<a class="back-link" href="${appPath("/admin")}">← Admin</a>
      <section class="comp-hero"><p class="eyebrow">Asking-price sample · ${esc(prettyDate(data.summary.observed_at))}</p>
        <h1>Local product price samples</h1>
        <p class="lead">${esc(rows.length)} distinct listings from ${esc(data.summary.requested_zips.length)} ZIP areas, checked within up to ${esc(data.summary.radius_miles)} miles of each search center. ${esc(data.summary.approved_count)} asking prices are in the estimate pool; ${esc(data.summary.needs_review_count)} cards need review. Medians use seller asking prices, not completed sale amounts. Seller towns give approximate distances.</p></section>
      <div class="comp-summary-grid">${types.map(([type, stats]) => `<div class="panel comp-summary"><span>${esc(cleanLabel(type))}</span><strong>${esc(money(stats.median_asking_price))}</strong><small>Median of ${esc(stats.candidate_count)} estimate references · middle half ${esc(money(stats.middle_half_asking_range[0]))}–${esc(money(stats.middle_half_asking_range[1]))}${stats.candidate_count < 3 ? " · Need at least 3 for an estimate" : ""}</small></div>`).join("")}</div>
      ${mowerCategories.length ? `<details class="panel coverage-panel" open><summary>Mower category coverage · ${esc(mowerCoverage.total_walk_behind)} reviewed walk-behind listings</summary>
        <p>Counts cover all sampled areas from the last year. Scout searches nearby references first, then widens the area for sparse mower variants. It still needs three matches for the same setup and a compatible condition before rating a listing. A category total alone does not qualify it.</p>
        <div class="coverage-grid">${mowerCategories.map(group => `<div><span>${esc(group.label)}</span><strong>${esc(group.count)}</strong></div>`).join("")}
          <div><span>Power or subtype unclear</span><strong>${esc(mowerCoverage.needs_category_review || 0)}</strong></div></div></details>` : ""}
      <p class="scan-notice">Prices are provisional. Scout uses at least three compatible asking prices before showing an estimate. Sparse mower variants may use broader-area references; condition and product details narrow the sample.</p>
      <div class="comp-controls"><label>Show <select id="general-status"><option value="approved" selected>Used for estimates</option><option value="candidate">Clear candidates</option><option value="review">Needs review</option><option value="all">All listings</option></select></label>
        <label>Type <select id="general-type"><option value="all">All types</option>${[...new Set(rows.map(row => row.reference_type))].sort().map(type => `<option value="${esc(type)}">${esc(cleanLabel(type))}</option>`).join("")}</select></label>
        <label>Scan ZIP <select id="general-zip"><option value="all">All areas</option>${data.summary.requested_zips.map(zip => `<option value="${esc(zip)}">${esc(zip)}</option>`).join("")}</select></label></div>
      <p id="general-count" class="comp-count"></p><div id="general-grid" class="results-grid"></div>`;
    const render = () => {
      const status = document.getElementById("general-status").value;
      const type = document.getElementById("general-type").value;
      const zip = document.getElementById("general-zip").value;
      const visible = rows.filter(row => (status === "all" ||
        (status === "approved" ? row.in_estimate_pool : row.review_status === status)) &&
        (type === "all" || row.reference_type === type) &&
        (zip === "all" || row.search_zip === zip));
      document.getElementById("general-count").textContent = plural(visible.length, "listing");
      document.getElementById("general-grid").innerHTML = visible.length
        ? visible.map(generalReferenceCard).join("") : '<div class="panel empty">No listings match these filters.</div>';
    };
    document.getElementById("general-status").addEventListener("change", render);
    document.getElementById("general-type").addEventListener("change", render);
    document.getElementById("general-zip").addEventListener("change", render);
    render();
  } catch (failure) {
    app.innerHTML = `<div class="panel empty">Could not load price samples: ${esc(failure.message)}</div>`;
  }
}

async function showHealth() {
  try {
    const data = await accountApi("/api/health");
    const recent = data.recent || {};
    const benchmark = data.benchmark || {};
    const measured = benchmark.metrics || {};
    const number = value => value == null ? "—" : String(value);
    const percent = value => value == null ? "—" : `${value}%`;
    const minutes = value => value == null ? "—" : `${value} min`;
    const start = Date.parse(benchmark.started_at || "");
    const end = Date.parse(benchmark.ends_at || "");
    const progress = Number.isFinite(start) && Number.isFinite(end) && end > start
      ? Math.max(0, Math.min(100, 100 * (Date.now() - start) / (end - start))) : 0;
    const states = {on_track: "On track", attention: "Needs attention",
      running: "Running", queued: "Queued", waiting: "Waiting for first check"};
    app.innerHTML = `<a class="back-link" href="${appPath("/")}">← Your searches</a>
      <section class="comp-hero"><p class="eyebrow">Search reliability</p><h1>Scan health</h1>
        <p class="lead">See when each saved search last worked, what Scout checked, and what still needs another look.</p></section>
      ${!data.configured ? `<div class="panel empty">Open a profile and save a search to start measuring it.
        <p><a class="open-link" href="${appPath("/setup")}">Set up a search →</a></p></div>` : `
      <section aria-labelledby="health-recent-title">
        <div class="section-heading"><div><p class="eyebrow">Last 48 hours</p><h2 id="health-recent-title">Actual scan results</h2></div></div>
        <div class="health-stats">
          <div class="panel health-stat"><span>Completed checks</span><strong>${esc(recent.complete || 0)} / ${esc(recent.attempts || 0)}</strong><small>${esc(percent(recent.completion_percent))} of finished attempts</small></div>
          <div class="panel health-stat"><span>Failed checks</span><strong>${esc(recent.failed || 0)}</strong><small>Includes interrupted scans</small></div>
          <div class="panel health-stat"><span>New alerts</span><strong>${esc(recent.new_alerts || 0)}</strong><small>Saved in your alert feed</small></div>
          <div class="panel health-stat"><span>Unfinished item analyses</span><strong>${esc(recent.known_unfinished_analyses || 0)}</strong><small>Counted on scans since this tracking began</small></div>
        </div>
        <p class="health-method">${esc(recent.cards_checked || 0)} visible cards checked · median completed scan ${esc(minutes(recent.median_scan_minutes))} · median scan-start-to-alert ${esc(minutes(recent.median_scan_to_alert_minutes))}. ${esc(recent.feedback_needs_correction || 0)} of ${esc(recent.feedback_rated || 0)} rated alerts received a Needs correction vote. Feedback is optional and does not establish overall accuracy.</p>
      </section>
      <section class="health-section" aria-labelledby="health-watch-title">
        <div class="section-heading"><div><p class="eyebrow">Your saved searches</p><h2 id="health-watch-title">Checks by keyword</h2></div></div>
        <div class="health-watch-grid">${data.watches?.length ? data.watches.map(watch => {
          const state = states[watch.state] || "Check status unknown";
          const link = watch.last_scan_id
            ? `<a class="open-link" href="${appPath(`/scan/${encodeURIComponent(watch.last_scan_id)}`)}">Open last scan →</a>` : "";
          return `<article class="panel health-watch-card">
            <div class="health-watch-head"><div><p class="eyebrow">${esc(watch.zip_code)} · ${esc(watch.radius_miles)} miles</p><h3>${esc(watch.term)}</h3></div>
              <span class="health-state ${esc(watch.state)}">${esc(state)}</span></div>
            <p>${esc(watch.note)}</p>
            <dl class="health-facts">
              <div><dt>Last attempt</dt><dd>${esc(watch.last_attempt_at ? prettyDate(watch.last_attempt_at) : "None yet")}</dd></div>
              <div><dt>Last completed</dt><dd>${esc(watch.last_success_at ? prettyDate(watch.last_success_at) : "None yet")}</dd></div>
              <div><dt>Next scheduled window</dt><dd>${esc(watch.next_scheduled_at ? prettyDate(watch.next_scheduled_at) : "Not scheduled")}</dd></div>
              <div><dt>Last cards checked</dt><dd>${esc(number(watch.last_cards_checked))}</dd></div>
              <div><dt>Last analysis needing retry</dt><dd>${esc(number(watch.last_unfinished_analyses))}</dd></div>
              <div><dt>48-hour checks</dt><dd>${esc(watch.recent.complete || 0)} complete · ${esc(watch.recent.failed || 0)} failed</dd></div>
            </dl>
            ${watch.last_error ? `<p class="health-error">${esc(watch.last_error)}</p>` : ""}
            ${link}
          </article>`;
        }).join("") : '<div class="panel empty">No saved searches yet.</div>'}</div>
      </section>
      <section class="health-section" aria-labelledby="health-benchmark-title">
        <div class="section-heading"><div><p class="eyebrow">14-day measurement</p><h2 id="health-benchmark-title">Scout benchmark</h2></div></div>
        <div class="panel health-benchmark">
          ${benchmark.status === "not_started" ? '<p>The benchmark has not started yet.</p>' : `
          <p><strong>${esc(benchmark.enrolled_searches || 0)} of ${esc(benchmark.target_searches)} target searches enrolled.</strong> Current saved keywords join automatically. This comparison includes only scans using their current ZIP, radius, and price settings.</p>
          <div class="research-meter" role="progressbar" aria-label="Benchmark time elapsed" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(progress)}"><span data-bar-width="${Math.round(progress)}"></span></div>
          <p class="health-method">Started ${esc(prettyDate(benchmark.started_at))} · ends ${esc(prettyDate(benchmark.ends_at))}</p>
          <div class="health-benchmark-metrics">
            <div><span>Checks completed</span><strong>${esc(measured.complete || 0)} / ${esc(measured.attempts || 0)}</strong></div>
            <div><span>New alerts</span><strong>${esc(measured.new_alerts || 0)}</strong></div>
            <div><span>90% of completed scans finished within</span><strong>${esc(minutes(measured.p90_scan_minutes))}</strong></div>
            <div><span>90% of app alerts arrived within</span><strong>${esc(minutes(measured.p90_scan_to_alert_minutes))}</strong><small>From scan start</small></div>
          </div>
          <p class="health-method">Needs correction votes: ${esc(measured.feedback_needs_correction || 0)} of ${esc(measured.feedback_rated || 0)} rated alerts. Scan-to-alert time starts when Scout begins a check; seller posting time remains unknown.</p>
          <p class="health-method"><strong>Competitor comparison: not measured.</strong> Scout alone cannot tell us which listings another service found or missed. Seller posting time is also not captured yet.</p>
          <a class="button button-secondary" href="${appPath("/api/health/export.csv")}" download="scout-benchmark.csv">Download Scout measurements</a>`}
        </div>
      </section>`}`;
    applyBarPositions(app);
  } catch (failure) {
    app.innerHTML = `<div class="panel empty">Could not load scan health: ${esc(failure.message)}</div>`;
  }
}

async function showPriceResearch() {
  app.innerHTML = `<a class="back-link" href="${appPath("/admin")}">← Admin</a>
    <section class="comp-hero"><p class="eyebrow">Pricing coverage</p>
      <h1>Price research</h1>
      <p class="lead">Scout tracks items with fewer than three compatible asking prices. Clearly identified mower and scooter models get bounded searches in new areas. Items with unclear details wait for review.</p></section>
    <div id="research-summary" class="comp-summary-grid"></div>
    <div class="comp-controls"><label>Show <select id="research-filter">
      <option value="all">All jobs</option><option value="active">Queued and running</option>
      <option value="review">Needs review</option><option value="complete">Complete</option>
    </select></label><span id="research-updated" class="comp-count"></span></div>
    <div id="research-jobs" class="research-grid"><div class="panel empty">Loading research jobs…</div></div>
    <p class="caution">Research uses seller asking prices, not completed sale prices. Scout stops after six different areas per category and at most twelve area checks per day.</p>`;
  let jobs = [];
  const filter = document.getElementById("research-filter");
  const render = () => {
    const counts = jobs.reduce((all, job) => {
      (all[job.status] ||= []).push(job);
      return all;
    }, {});
    const active = (counts.queued?.length || 0) + (counts.running?.length || 0) +
      (counts.waiting?.length || 0);
    document.getElementById("research-summary").innerHTML =
      `<div class="panel comp-summary"><span>In research</span><strong>${esc(active)}</strong><small>Queued, checking, or waiting</small></div>
       <div class="panel comp-summary"><span>Need item review</span><strong>${esc(counts.needs_review?.length || 0)}</strong><small>Not enough detail for a safe search</small></div>
       <div class="panel comp-summary"><span>Complete</span><strong>${esc(counts.complete?.length || 0)}</strong><small>At least three compatible prices</small></div>`;
    const visible = jobs.filter(job => filter.value === "all" ||
      (filter.value === "active" ? ["queued", "running", "waiting", "exhausted"].includes(job.status) :
       filter.value === "review" ? ["needs_review", "exhausted"].includes(job.status) :
       job.status === "complete"));
    document.getElementById("research-jobs").innerHTML = visible.length
      ? visible.map(job => {
        const latest = job.attempts?.at(-1);
        const target = job.targets?.[0];
        const scanLink = target ? `${appPath(`/scan/${target.scan_id}`)}#item-${encodeURIComponent(target.listing_id)}` : "";
        const state = {queued: "Queued", running: "Checking now", waiting: "Next area scheduled",
          needs_review: "Needs item review", exhausted: "Search limit reached", complete: "Enough prices"}[job.status] || job.status;
        return `<article class="panel research-card">
          <div class="research-card-head"><div><span class="eyebrow">${esc(job.origin_zip)} · ${esc(job.current_area || "Price coverage")}</span>
            <h2>${esc(job.title)}</h2></div><span class="research-status ${esc(job.status)}">${esc(state)}</span></div>
          <div class="research-meter" role="meter" aria-label="Compatible asking prices found" aria-valuemin="0" aria-valuemax="3" aria-valuenow="${esc(Math.min(3, job.price_points || 0))}">
            <span style="width:${Math.min(100, Math.max(0, (job.price_points || 0) / 3 * 100))}%"></span></div>
          <p class="research-points"><strong>${esc(job.price_points || 0)} of 3</strong> usable asking prices for this item · ${esc(job.attempts?.length || 0)} of 6 areas checked</p>
          <p>${esc(job.note || "Waiting for a new search area")}</p>
          ${job.status === "waiting" && job.next_attempt_at ? `<p class="comp-meta">Next area after ${esc(prettyDate(job.next_attempt_at))}</p>` : ""}
          ${latest ? `<details class="research-history"><summary>Latest area: ${esc(latest.area)} ${esc(latest.zip_code)} · ${esc(latest.status)}</summary>
            <p>${esc(latest.approved_count || 0)} model-matched references saved · ${esc(latest.candidate_count || 0)} clear candidates. The usable count above also checks condition.</p>
            ${latest.error ? `<p class="comp-review">${esc(latest.error)}</p>` : ""}</details>` : ""}
          ${scanLink ? `<a class="open-link" href="${esc(scanLink)}">Open original item →</a>` : ""}
        </article>`;
      }).join("") : '<div class="panel empty">No research jobs in this view.</div>';
  };
  filter.addEventListener("change", render);
  const refresh = async () => {
    try {
      const data = await api("/api/price-research");
      jobs = data.jobs || [];
      document.getElementById("research-updated").textContent =
        `${jobs.length} tracked categories · updated ${prettyDate(data.updated_at)}`;
      render();
    } catch (error) {
      document.getElementById("research-jobs").innerHTML =
        `<div class="panel empty">Could not load research jobs: ${esc(error.message)}</div>`;
    }
  };
  await refresh();
  setInterval(() => { if (pagePath === "/price-research") refresh(); }, 30000);
}

if (pagePath === "/health") showHealth();
else if (pagePath === "/price-research") showPriceResearch();
else if (pagePath === "/comparables/general") showGeneralComparables();
else if (pagePath === "/comparables/couches") showCouchComparables();
else if (publicReadOnly && !scanPath) showPublicHome();
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
    list.innerHTML = history.scans.length ? history.scans.map(scan => historyRow(scan, true)).join("")
      : '<div class="panel empty">No scans yet. Start with your ZIP code above.</div>';
  } catch { /* Keep the current archive until the next refresh. */ }
}, 8000);

if (!publicReadOnly && pagePath === "/health") setInterval(showHealth, 30000);
