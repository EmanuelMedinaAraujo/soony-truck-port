const $ = id => document.getElementById(id);
const html = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[char]);
const numbers = new Intl.NumberFormat("en-GB", {maximumFractionDigits: 2});
const euros = new Intl.NumberFormat("en-GB", {style: "currency", currency: "EUR", maximumFractionDigits: 0});
const time = new Intl.DateTimeFormat("en-GB", {timeZone: "Europe/Berlin", hour: "2-digit", minute: "2-digit"});
const weekday = new Intl.DateTimeFormat("en-GB", {timeZone: "Europe/Berlin", weekday: "short"});
const at = date => `${weekday.format(new Date(date))} ${time.format(new Date(date))}`;
const number = value => value == null ? "Unconfirmed" : numbers.format(value);
const money = value => value == null ? "Unconfirmed" : euros.format(value);

// Component and edition artwork extracted from the supplied deep-dive deck, slides 17–19.
const artwork = {
  "CMP-GAM-001": ["game-card", "Game card"], "CMP-GAM-003": ["clear-case", "Clear case"],
  "CMP-GAM-004": ["sleeve", "Cover sleeve"], "CMP-GAM-006": ["steel-case", "Steel case"],
  "CMP-GAM-007": ["box", "Collector’s box"], "CMP-GAM-008": ["art-book", "Art book"],
  "CMP-GAM-009": ["poster", "Map poster"], "CMP-GAM-010": ["statue", "Statue"],
  "CMP-GAM-011": ["leaflet", "Code leaflet"], "CMP-MAG-005": [null, "Lid magnet"],
};
const editionArtwork = {"FG-GH6-CE": "collector-edition", "FG-GH6-STD": "standard-edition"};
const states = {covered: "Covered", short: "Short on site", verify: "Check record", confirmed: "Confirmed",
  pending: "Pending", blocked: "Blocked", unknown: "Unconfirmed", assumed: "Assumed confirmed"};
const negotiator = "https://github.com/ManuelLerchner/HACKLeague2026-lio";
const art = (id, className = "art", type = "webp") => id ? `<img class="${className}" src="/assembly/art/${id}.${type}" alt="">` : "";
const nameOf = component => artwork[component.id]?.[1] ?? component.name;
const editionName = id => state.editions.find(edition => edition.id === id)?.name ?? "Edition";

let state;
let selection = null;
let routeSelection = null;
let assumeConfirmation = true;
let overviewKey = "";
let inspectorKey = "";
let requestOpen = false;
let mapLand;
let loadingMap = false;
const premiums = new Map();

function partArt(component) {
  return art(artwork[component.id]?.[0]) || (component.includedIn ? '<i class="magnet-icon" aria-hidden="true"></i>' : "");
}

function partButton(component) {
  const status = states[component.status] ?? "Check status";
  const fittedParts = state.components.filter(part => part.includedIn?.componentId === component.id);
  const tile = `<button type="button" class="part" data-part="${html(component.id)}" data-state="${html(component.status)}"
    aria-expanded="${component.id === selection}" aria-controls="inspector">
    ${partArt(component)}<span class="part-title"><i class="status-dot ${html(component.status)}" aria-hidden="true"></i>${html(nameOf(component))}
    <span class="sr-only">, ${html(status)}</span></span></button>`;
  return fittedParts.length ? `<div class="part-assembly">${tile}<div class="fitted-parts" aria-label="Fitted in ${html(nameOf(component))}">${fittedParts.map(part =>
    `<button type="button" class="fitted-part" data-part="${html(part.id)}" aria-expanded="${part.id === selection}" aria-controls="inspector">${partArt(part)}${html(nameOf(part))}</button>`).join("")}</div></div>` : tile;
}

function lane(edition, parts) {
  const columns = parts.length === 2 ? " two" : parts.length === 5 ? "" : " variable";
  return `<section class="lane" data-edition="${html(edition.id)}" aria-label="${html(edition.name)} supply">
    <div class="parts${columns}">${parts.map(partButton).join("")}</div>${art("arrow", "arrow")}
    <div class="result">${art(editionArtwork[edition.id])}<h2>${html(edition.name)}</h2>
      <p class="count" aria-label="${number(edition.projected)} of ${number(edition.demand)} projected">${number(edition.projected)} / ${number(edition.demand)}</p></div>
  </section>`;
}

function drawLinks() {
  const graphic = $("assembly");
  const shared = graphic.querySelector(".shared");
  const wiring = graphic.querySelector(".wiring");
  if (!shared || !wiring) return;
  const edges = graphic.getBoundingClientRect();
  const source = shared.getBoundingClientRect();
  const startX = source.right - edges.left;
  const startY = source.top + source.height / 2 - edges.top;
  const chosen = state.components.find(part => part.id === selection);
  const dests = graphic.querySelectorAll(".lane");
  wiring.innerHTML = [...dests].map(dest => {
    const edge = dest.querySelector(".parts").getBoundingClientRect();
    const x = edge.left - edges.left;
    const y = edge.top + edge.height / 2 - edges.top;
    const elbow = startX + (x - startX) / 2;
    return `<path d="M${startX} ${startY} H${elbow} V${y} H${x}"${chosen?.shared ? ' class="is-active"' : ""}/>`;
  }).join("");
}

function updateOverview() {
  const next = JSON.stringify([state.components.map(c => [c.id, c.name, c.status, c.shared, c.includedIn, c.allocations.map(a => a.editionId)]),
    state.editions.map(e => [e.id, e.name, e.projected, e.demand])]);
  if (next !== overviewKey) {
    const focus = document.activeElement?.dataset.part;
    overviewKey = next;
    const shared = state.components.filter(part => part.shared && !part.includedIn);
    const lanes = state.editions.map(edition => lane(edition, state.components.filter(part =>
      !part.shared && !part.includedIn && part.allocations.some(allocation => allocation.editionId === edition.id))));
    $("assembly").innerHTML = `<svg class="wiring" aria-hidden="true"></svg>${shared.length ? `<section class="shared" aria-label="Shared by both editions">
      <h2>Shared by both</h2><div class="shared-parts">${shared.map(partButton).join("")}</div></section>` : ""}${lanes.join("")}`;
    if (focus) partControl(focus)?.focus({preventScroll: true});
  }
  $("initial").hidden = true;
  $("assembly").hidden = false;
  $("legend").hidden = false;
  $("deadline").textContent = at(state.deadline);
  $("cutoff").textContent = `Receiving cutoff ${time.format(new Date(state.cutoff))}`;
  drawLinks();
}

function sourceList(entries, label = "Source evidence") {
  if (!entries?.length) return "";
  return `<details class="detail-block" data-detail="${html(label)}"><summary>${html(label)}</summary><ul>${entries.map(entry =>
    `<li><strong>${html(entry.label)}</strong><br>${html(entry.detail)}</li>`).join("")}</ul></details>`;
}

function process(part, route, offer) {
  const find = pattern => route?.steps.find(step => pattern.test(step.name));
  const hasOrder = route && !route.id.startsWith("stock-");
  const assumed = (offer || route?.tracking?.status === "booked") && assumeConfirmation;
  const recordedOrder = offer ? "pending" : hasOrder ? "confirmed" : "unknown";
  const transport = find(/truck pickup|^transport$/i) ?? find(/port arrival|supplier release/i);
  const receiving = find(/receiving/i) ?? find(/warehouse stock/i);
  const deadline = Date.parse(state.deadline) > Date.parse(state.now) ? at(state.deadline) : "Deadline passed";
  const stage = (icon, label, status, detail) => `<li data-state="${status}">${art(icon, "process-icon", "png")}<strong>${label}</strong><span>${html(detail ?? states[status])}</span></li>`;
  return [
    stage("order", "Order", assumed && offer ? "assumed" : recordedOrder, assumed && offer ? "In scenario" : offer ? "Order pending" : recordedOrder === "confirmed" ? "Shipment record" : "Stock record"),
    stage("confirm", "Confirm", assumed ? "assumed" : offer ? "pending" : "unknown", assumed ? "Assumed confirmed" : offer ? "Confirmation open" : "See supplier evidence"),
    stage("ship", "Ship", transport?.state ?? "unknown", transport ? states[transport.state] : "See route"),
    stage("track", "Track", "pending", offer ? "Awaiting carrier ETA" : "See route evidence"),
    stage("receive", "Receive", receiving?.state ?? "unknown", receiving ? states[receiving.state] : "Receipt unconfirmed"),
    stage("pack", "Pack", part.status === "covered" ? "confirmed" : "pending", part.status === "covered" ? "Part covered" : "Supply gap open"),
    stage("dispatch", "Dispatch", "pending", deadline),
  ].join("");
}

const shipmentStates = {in_transit:"In transit", in_delivery:"Out for delivery", arrived_port:"At port", delayed:"Delayed",
  booked:"Booked", delivered:"Delivered", arrived_dc:"At the warehouse"};

function routeSummary(part, route) {
  const tracking = route?.tracking;
  if (tracking) {
    if (tracking.hold) return ["Waiting for supplier release", tracking.hold.split(" -> ")[1] ?? "Dockside is checking what the supplier needs before dispatch."];
    if (tracking.status === "arrived_port") return [`At the port in ${tracking.terminal}`, "Confirm a truck pickup for the warehouse."];
    if (tracking.status === "booked") return ["Booked, departure unconfirmed", "Get a departure update from the carrier."];
    if (["delivered", "arrived_dc"].includes(tracking.status)) return ["At the warehouse", "Check the receipt and inspection records for usable stock."];
    if (tracking.arrival && Date.parse(tracking.arrival) < Date.parse(state.now)) return ["Receipt unconfirmed", `The shipment was due ${at(tracking.arrival)}. Check with the carrier.`];
    return [shipmentStates[tracking.status] ?? "Shipment on file", tracking.arrival ?
      `Estimated warehouse arrival ${at(tracking.arrival)}.` : "A warehouse arrival time is still open."];
  }
  if (part.option && route?.id.startsWith("offer-")) {
    const option = part.option;
    if (option.timingState === "expired") return ["The rush order window has passed", `${option.supplier} offered ${number(option.quantity)} units. This route misses the modeled receiving cutoff.`];
    return ["Waiting for a truck and dock slot", `${option.supplier} has offered ${number(option.quantity)} units. Verify a legal vehicle and agree on a Hamburg unload time.`];
  }
  if (part.status === "verify") return ["Checking the part record", "The records do not establish whether Hamburg stocks this part separately."];
  return part.status === "short" ? ["Short on site", "Dockside needs an inbound shipment or a replacement offer for this part."] :
    ["Covered by warehouse stock", "The usable stock in Hamburg covers the current requirement."];
}

function renderRoute(route) {
  const evidence = sourceList(route.evidence);
  const checks = route.steps.length ? `<details class="detail-block" data-detail="route checks"><summary>Shipment records and checks</summary>
    <ul class="handoffs">${route.steps.map(step => `<li><b>${html(step.name)}</b><span class="state">${html(states[step.state] ?? states.unknown)}</span><p>${html(step.detail)}</p></li>`).join("")}</ul>${evidence}</details>` : evidence;
  return checks;
}

async function loadMap() {
  if (mapLand !== undefined || loadingMap) return;
  loadingMap = true;
  try {
    const response = await fetch("/assembly/map-land.json");
    if (!response.ok) throw new Error(response.status);
    mapLand = await response.json();
  } catch {
    mapLand = [];
  } finally {
    loadingMap = false;
    const tracking = activeRoute()?.tracking;
    const target = $("transport-map");
    if (target && tracking) target.innerHTML = mapFigure(tracking, mapLand);
  }
}

function optionView(part) {
  const option = part.option;
  if (!option) return "";
  const valid = option.latestUsefulAt && option.timingState === "open";
  const timing = option.timingState === "expired" ? "Estimated order window has passed." :
    valid ? `Estimated latest order release ${at(option.latestUsefulAt)}.` : "A feasible release time still needs confirmation.";
  return `<h3>Recovery if received in time</h3>
    <dl class="kpi"><dt>Rush order</dt><dd>${money(option.cost)}</dd>
    <dt>Extra ${html(editionName(option.editionId))} copies</dt><dd>${number(option.copiesAdded)}</dd>
    <dt>Refunds avoided</dt><dd>${money(option.refundsAvoided)}</dd></dl>
    <p class="side-note">${html(timing)}${option.nextConstraint ? ` The next constraint is ${html(option.nextConstraint.toLowerCase())}.` : ""}</p>
    ${option.assurance && option.refundsAvoided > 0 ? `<details class="detail-block" data-detail="standby"><summary>Compare standby transport</summary>
      <label class="premium" for="premium">Example premium in euros <input id="premium" type="number" min="0" step="any" inputmode="decimal"
        value="${html(premiums.get(part.id) ?? option.assurance.examplePremium)}"></label>
      <p id="break-even" class="side-note" aria-live="polite"></p></details>` : ""}`;
}

function updatePremium() {
  const input = $("premium");
  const result = $("break-even");
  if (!input || !result) return;
  const part = state.components.find(c => c.id === selection);
  const value = input.valueAsNumber;
  if (!Number.isFinite(value) || value < 0) {
    result.textContent = "Enter a non-negative premium to compare.";
    return;
  }
  const points = value / part.option.refundsAvoided * 100;
  const requirement = points > 100 ? "This premium exceeds the refunds the recovery could avoid." :
    `Standby breaks even if it increases the chance of on-time receipt by ${number(points)} percentage ${points === 1 ? "point" : "points"}.`;
  result.textContent = `${requirement} Both routes still need a usable receipt and a dock slot.`;
}

function activePart() {
  return state?.components.find(component => component.id === selection);
}

function routesFor(part) {
  const parent = state.components.find(component => component.id === part.includedIn?.componentId);
  return parent?.routes ?? part.routes;
}

function activeRoute() {
  const part = activePart();
  if (!part) return null;
  const routes = routesFor(part);
  return routes.find(route => route.id === routeSelection) ?? routes.find(route => route.id.startsWith("offer-")) ??
    routes.find(route => route.tracking && !route.tracking.hold && ["arrived_port", "in_transit", "in_delivery", "delayed"].includes(route.tracking.status)) ?? routes[0];
}

function routePicker(routes, active) {
  if (routes.length < 2) return "";
  return `<label class="route-picker" for="route-picker">Shipment or source<select id="route-picker">${routes.map(route => {
    const tracking = route.tracking;
    const label = tracking ? `${tracking.reference} · ${shipmentStates[tracking.status] ?? "Shipment"}` : route.name;
    return `<option value="${html(route.id)}"${route.id === active?.id ? " selected" : ""}>${html(label)}</option>`;
  }).join("")}</select></label>`;
}

function renderInspector(force = false) {
  const inspector = $("inspector");
  const part = activePart();
  inspector.hidden = !part;
  if (!part) return;
  const route = activeRoute();
  const routes = routesFor(part);
  const key = JSON.stringify([part, routes, route?.id, state.deadline, assumeConfirmation]);
  if (!force && (key === inspectorKey || inspector.contains(document.activeElement))) return;
  inspectorKey = key;
  const offer = Boolean(part.option && route?.id.startsWith("offer-"));
  const showConfirmation = offer || route?.tracking?.status === "booked";
  const parent = state.components.find(component => component.id === part.includedIn?.componentId);
  const stockText = part.status === "verify" ? "Part record needs checking" : `${number(part.usable)} usable in Hamburg · ${number(part.required)} needed`;
  const partNote = parent ? `Fitted into the ${nameOf(parent)} by ${part.includedIn.supplier}.` : stockText;
  const [headline, next] = routeSummary(parent ?? part, route);
  const wasOpen = new Set([...inspector.querySelectorAll("details[open]")].map(node => node.dataset.detail));
  inspector.innerHTML = `<div class="inspector-head">${partArt(part)}
      <div><h2 id="part-heading" tabindex="-1">${html(nameOf(part))}</h2>
      <p>${html(partNote)}</p></div>
      <button class="text-button" id="close-part" type="button">Close trace</button></div>
    <div class="shipment-head"><div class="shipment-summary"><h3>${html(headline)}</h3><p>${html(next)}</p></div>${routePicker(routes, route)}</div>
    ${route?.tracking ? `<div id="transport-map" class="transport-view" aria-label="Transport map">${mapLand !== undefined ? mapFigure(route.tracking, mapLand) : '<p class="side-note">Loading the shipment map.</p>'}</div>` : ""}
    ${parent ? sourceList([part.includedIn.evidence], "Supplier fitting record") : ""}
    ${showConfirmation ? `<div class="handoff"><label><input id="assume-confirmed" type="checkbox" ${assumeConfirmation ? "checked" : ""}> Assume supplier confirms the order</label>
      <a href="${negotiator}" target="_blank" rel="noreferrer">Order negotiator</a>
      ${assumeConfirmation ? "<p>Scenario assumption. Transport capacity, the dock slot and receipt use live evidence.</p>" : offer ? "<p>Live order confirmation is pending.</p>" : ""}</div>` : ""}
    <ol class="process" id="process" aria-label="Seven steps to dispatch">${process(parent ?? part, route, offer)}</ol>
    <div class="info-grid"><div>${route ? renderRoute(route) : "<p>There is no shipment record for this part yet.</p>"}
    ${part.adjustments.length ? `<details class="detail-block" data-detail="inventory checks"><summary>Inventory checks</summary><ul>${part.adjustments.map(adjustment =>
      `<li>${html(adjustment.detail)}${adjustment.evidence.length ? `<br><small>${adjustment.evidence.map(html).join(", ")}</small>` : ""}</li>`).join("")}</ul></details>` : ""}</div>
    <div>${offer ? optionView(part) : ""}${parent ? `<p class="side-note">Hamburg receives and counts this magnet as part of the ${html(nameOf(parent))}.</p>` :
    `<details class="detail-block" data-detail="allocation"><summary>Edition allocations</summary><ul>${part.allocations.map(allocation =>
      `<li>${html(editionName(allocation.editionId))} · ${number(allocation.allocated)} of ${number(allocation.required)} allocated</li>`).join("")}</ul></details>`}</div></div>`;
  for (const node of inspector.querySelectorAll("details")) if (wasOpen.has(node.dataset.detail)) node.open = true;
  updatePremium();
  if (route?.tracking) loadMap();
}

function partControl(id) {
  return document.querySelector(`[data-part="${CSS.escape(id)}"]`);
}

function selectPart(id) {
  const previous = selection;
  selection = id;
  routeSelection = null;
  for (const node of document.querySelectorAll("[data-part]")) node.setAttribute("aria-expanded", String(node.dataset.part === selection));
  drawLinks();
  renderInspector(true);
  if (selection) {
    $("inspector").scrollIntoView({block: "start"});
    $("part-heading").focus({preventScroll: true});
  }
  else if (previous) partControl(previous)?.focus();
}

async function refresh() {
  if (requestOpen) return;
  requestOpen = true;
  try {
    const response = await fetch("/api/dispatch", {cache: "no-store"});
    if (!response.ok) throw new Error(response.status);
    state = await response.json();
    $("sync").textContent = state.checkedAt ? `Checked ${time.format(new Date(state.checkedAt))}` : "Loading portal data";
    updateOverview();
    renderInspector();
    renderOperations(state);
  } catch {
    $("sync").textContent = state ? "Refresh failed" : "Disconnected";
    if (!state) $("initial").textContent = "Can’t load the assembly plan. Check the server or refresh this page.";
  } finally {
    requestOpen = false;
  }
}

$("overview").addEventListener("click", event => {
  const part = event.target.closest("[data-part]");
  if (part) selectPart(selection === part.dataset.part ? null : part.dataset.part);
});
$("inspector").addEventListener("click", event => {
  if (event.target.closest("#close-part")) selectPart(null);
});
$("inspector").addEventListener("change", event => {
  if (event.target.id === "assume-confirmed") assumeConfirmation = event.target.checked;
  else if (event.target.id === "route-picker") routeSelection = event.target.value;
  else return;
  const control = event.target.id;
  renderInspector(true);
  $(control).focus({preventScroll: true});
});
$("inspector").addEventListener("input", event => {
  if (event.target.id !== "premium") return;
  premiums.set(selection, event.target.value);
  updatePremium();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && selection && $("inspector").contains(document.activeElement)) selectPart(null);
});
new ResizeObserver(drawLinks).observe($("assembly"));
initOperations(refresh);
refresh();
setInterval(refresh, 15000);
