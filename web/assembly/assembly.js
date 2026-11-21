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
let openedFromNextSteps = false;
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
  const selectedTile = activePart()?.includedIn?.componentId ?? selection;
  return `<button type="button" class="part" data-part="${html(component.id)}" data-state="${html(component.status)}"
    aria-expanded="${component.id === selectedTile}" aria-controls="inspector">
    ${partArt(component)}<span class="part-title"><i class="status-dot ${html(component.status)}" aria-hidden="true"></i>${html(nameOf(component))}
    <span class="sr-only">, ${html(status)}</span></span></button>`;
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

const stages = [["order", "Order"], ["confirm", "Confirm"], ["ship", "Ship"], ["track", "Track"],
  ["receive", "Receive"], ["pack", "Pack"], ["dispatch", "Dispatch"]];

function process(currentStage) {
  const current = stages.findIndex(([id]) => id === currentStage);
  return stages.map(([id, label], index) => {
    const position = index < current ? "past" : index === current ? "current" : "future";
    return `<li data-position="${position}"${index === current ? ' aria-current="step"' : ""}>
      <i class="stage-marker" aria-hidden="true"></i>${art(id, "process-icon", "png")}<strong>${label}</strong></li>`;
  }).join("");
}

const shipmentStates = {in_transit:"In transit", in_delivery:"Out for delivery", arrived_port:"At port", delayed:"Delayed",
  booked:"Booked", delivered:"Delivered", arrived_dc:"At the warehouse"};

function renderRouteDetails(route, showConfirmation) {
  if (!route) return "";
  return `<details class="detail-block shipment-details" data-detail="route checks"><summary>Details</summary>
    <ul class="handoffs">${route.steps.map(step => `<li><b>${html(step.name)}</b><span class="state">${html(states[step.state] ?? states.unknown)}</span><p>${html(step.detail)}</p></li>`).join("")}</ul>
    ${sourceList(route.evidence)}
    ${showConfirmation ? `<div class="handoff"><label><input id="assume-confirmed" type="checkbox" ${assumeConfirmation ? "checked" : ""}> Assume supplier confirms the order</label>
      <a href="${negotiator}" target="_blank" rel="noreferrer">Order negotiator</a>
      <p>${assumeConfirmation ? "The scenario assumes supplier confirmation. Transport and receipt still need evidence." : "The scenario leaves supplier confirmation open."}</p></div>` : ""}
    </details>`;
}

function renderTransportMap() {
  const route = activeRoute();
  const target = $("transport-map");
  if (target && route?.tracking && mapLand !== undefined) {
    target.innerHTML = mapFigure(route.tracking, mapLand, route.id);
  }
}

async function loadMap() {
  loadTrackingDetails(renderTransportMap);
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
    renderTransportMap();
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
  const part = activePart();
  const parent = state.components.find(component => component.id === part.includedIn?.componentId);
  const value = input.valueAsNumber;
  if (!Number.isFinite(value) || value < 0) {
    result.textContent = "Enter a non-negative premium to compare.";
    return;
  }
  const points = value / (parent ?? part).option.refundsAvoided * 100;
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
  return routes.find(route => route.id === (routeSelection ?? part.default_route_id)) ??
    routes.find(route => route.id === part.default_route_id) ?? routes[0];
}

function routePicker(routes, active) {
  if (routes.length < 2) return "";
  return `<label class="route-picker" for="route-picker">Shipment or source<select id="route-picker">${routes.map(route => {
    const tracking = route.tracking;
    const label = tracking ? `${shipmentStates[tracking.status] ?? "Shipment"} (${tracking.reference})` : route.name;
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
  const key = JSON.stringify([part, routes, route?.id, assumeConfirmation]);
  if (!force && key === inspectorKey) return;
  const line = nextStepLine(routeSelection ? route?.next_step ?? part.next_step : part.next_step);
  const currentStage = route?.current_stage ?? part.current_stage;
  const focused = document.activeElement;
  const headingFocused = focused === $("part-heading");
  if (!force && !headingFocused && inspector.contains(focused)) {
    const summary = $("shipment-step");
    const flow = $("process");
    if (summary.dataset.step !== line) {
      summary.dataset.step = line;
      summary.innerHTML = line;
    }
    if (flow && flow.dataset.stage !== currentStage) {
      flow.dataset.stage = currentStage;
      flow.innerHTML = process(currentStage);
    }
    return;
  }
  inspectorKey = key;
  const parent = state.components.find(component => component.id === part.includedIn?.componentId);
  const offer = Boolean((parent ?? part).option && route?.id.startsWith("offer-"));
  const showConfirmation = offer || route?.tracking?.status === "booked";
  const stockText = part.status === "verify" ? "Part record needs checking" : `${number(part.usable)} usable in Hamburg, ${number(part.required)} needed`;
  const partNote = parent ? `Fitted into the ${nameOf(parent)} by ${part.includedIn.supplier}.` : stockText;
  const wasOpen = new Set([...inspector.querySelectorAll("details[open]")].map(node => node.dataset.detail));
  inspector.innerHTML = `<div class="inspector-head"><div class="inspector-banner">${partArt(part)}
      <div><h2 id="part-heading" tabindex="-1">${html(nameOf(part))}</h2>
      <p>${html(partNote)}</p></div>
      <button class="text-button" id="close-part" type="button">Close trace</button></div>
    <div class="shipment-head"><div class="shipment-summary"><h3>Dockside’s next step</h3>
      <p class="next-step-line" id="shipment-step" data-step="${html(line)}" aria-live="polite">${line}</p></div>
      ${renderRouteDetails(route, showConfirmation)}</div>
    </div>
    ${route?.tracking ? `<div id="transport-map" class="transport-view" aria-label="Transport map">${mapLand !== undefined ? mapFigure(route.tracking, mapLand, route.id) : '<p class="side-note">Loading the shipment map.</p>'}</div>` : ""}
    ${parent ? sourceList([part.includedIn.evidence], "Supplier fitting record") : ""}
    <div class="info-grid"><div>
    ${part.adjustments.length ? `<details class="detail-block" data-detail="inventory checks"><summary>Inventory checks</summary><ul>${part.adjustments.map(adjustment =>
      `<li>${html(adjustment.detail)}${adjustment.evidence.length ? `<br><small>${adjustment.evidence.map(html).join(", ")}</small>` : ""}</li>`).join("")}</ul></details>` : ""}</div>
    <div>${offer ? optionView(parent ?? part) : ""}${parent ? `<p class="side-note">Hamburg receives and counts this magnet as part of the ${html(nameOf(parent))}.</p>` :
    `<details class="detail-block" data-detail="allocation"><summary>Edition allocations</summary><ul>${part.allocations.map(allocation =>
      `<li>${html(editionName(allocation.editionId))} has ${number(allocation.allocated)} of ${number(allocation.required)} allocated</li>`).join("")}</ul></details>`}</div></div>`;
  for (const node of inspector.querySelectorAll("details")) if (wasOpen.has(node.dataset.detail)) node.open = true;
  window.PartDependencyGraph.mount(inspector, part.id, id => {
    const component = state.components.find(item => item.id === id);
    return component && nameOf(component);
  }, selectPart, route?.id, shipmentId => {
    if (!routes.some(item => item.id === shipmentId)) return false;
    routeSelection = shipmentId;
    renderInspector(true);
    document.querySelector("#transport-map")?.scrollIntoView({block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"});
    return true;
  });
  if (headingFocused) $("part-heading").focus({preventScroll: true});
  updatePremium();
  if (route?.tracking) loadMap();
}

function partControl(id) {
  const tileId = state.components.find(part => part.id === id)?.includedIn?.componentId ?? id;
  return document.querySelector(`[data-part="${CSS.escape(tileId)}"]`);
}

function selectPart(id, fromNextSteps = false) {
  const previous = selection;
  const nextStepsButton = openedFromNextSteps && previous &&
    $("next-list").querySelector(`[data-next-part="${CSS.escape(previous)}"]`);
  openedFromNextSteps = fromNextSteps;
  selection = id;
  routeSelection = null;
  const control = selection && partControl(selection);
  for (const node of document.querySelectorAll("[data-part]")) node.setAttribute("aria-expanded", String(node === control));
  drawLinks();
  renderInspector(true);
  if (selection) {
    $("inspector").scrollIntoView({block: "start"});
    $("part-heading").focus({preventScroll: true});
  }
  else if (previous) (nextStepsButton || partControl(previous))?.focus();
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
    if (activeRoute()?.tracking) loadMap();
    renderOperations(state);
    renderNextSteps(state);
  } catch {
    $("sync").textContent = state ? "Refresh failed" : "Disconnected";
    if (!state) $("initial").textContent = "Can’t load the assembly plan. Check the server or refresh this page.";
  } finally {
    requestOpen = false;
  }
}

$("next-list").addEventListener("click", event => {
  const part = event.target.closest("[data-next-part]");
  if (part) selectPart(part.dataset.nextPart, true);
});
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
  const part = activePart();
  premiums.set(part.includedIn?.componentId ?? part.id, event.target.value);
  updatePremium();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && selection && $("inspector").contains(document.activeElement)) selectPart(null);
});
new ResizeObserver(drawLinks).observe($("assembly"));
initOperations(refresh);
refresh();
refreshBusy();
setInterval(refresh, 15000);
setInterval(refreshBusy, 5000);
