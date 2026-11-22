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
const siteName = () => state?.siteName ?? "the site";
const editionName = id => state.editions.find(edition => edition.id === id)?.name ?? "Edition";

let state;
let selection = null;
let openedFromNextSteps = false;
let routeSelection = null;
let assumeConfirmation = true;
let overviewKey = "";
let inspectorKey = "";
let requestOpen = false;
let chaserCases = [];
let chaserRequestOpen = false;
let mapLand;
let loadingMap = false;

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

/* The one line a buyer needs first: what is at risk, in copies and money, and which part decides it. */
function renderStakes() {
  const short = state.editions.filter(edition => edition.short > 0);
  const lever = state.components.find(part => part.id === state.focusComponentId) ??
    state.components.find(part => part.id === short[0]?.constraintIds?.[0]);
  const parts = short.map(edition => `<b>${number(edition.short)}</b> ${html(edition.name)} ${edition.short === 1 ? "copy" : "copies"} short` +
    (edition.refundsAtRisk ? ` (${money(edition.refundsAtRisk)} in refunds)` : ""));
  const text = short.length ? `${parts.join(" · ")}${lever ? `. Biggest lever: <button class="next-part" type="button" data-stake-part="${html(lever.id)}">${html(nameOf(lever))}</button>` : ""}.`
    : "Every edition is covered for Sunday.";
  if ($("stakes").dataset.text !== text) { $("stakes").dataset.text = text; $("stakes").innerHTML = text; }
}

/* For a rush offer: what it costs, what it can add, and what still limits the edition afterwards. */
function renderEconomics(part, route) {
  const option = part.option;
  if (!option || !route?.id?.startsWith("offer-")) return "";
  const edition = state.editions.find(item => item.id === option.editionId);
  if (!edition) return "";
  const after = edition.projected + option.copiesAdded;
  const limit = option.nextConstraint ? `${html(option.nextConstraint)} then ${edition.demand - after > 0 ? `limits it: ${number(edition.demand - after)} still short` : "is not a limit"}.` : "";
  const ordered = route.steps?.some(step => step.name === "Formal order" && step.state !== "pending");
  const by = option.latestUsefulAt && !ordered ? ` Order by ${html(at(option.latestUsefulAt))} at the latest.` : "";
  return `<p class="economics"><b>If ${number(option.quantity)} arrive usable in time:</b> ${html(edition.name)} ${number(edition.projected)} → ${number(after)}
    (+${number(option.copiesAdded)}). ${money(option.refundsAvoided)} in refunds avoided for ${money(option.cost)}. ${limit}${by}</p>`;
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
  $("cutoff").textContent = `Last delivery into ${siteName()} ${time.format(new Date(state.cutoff))}`;
  renderStakes();
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

const stepStates = {confirmed: "Done", waiting: "Waiting for answer", pending: "To do", blocked: "Blocked", unknown: "Unclear"};

/* What Dockside does now, then the selected delivery's checkpoints on one vertical line. */
function renderProgress(route, line) {
  const now = `<p class="next-step-line progress-now" id="shipment-step" data-step="${html(line)}" aria-live="polite">${line}</p>`;
  if (!route?.steps?.length) return now;
  return `${now}<p class="progress-caption">Progress of ${html(route.name.charAt(0).toLowerCase() + route.name.slice(1))}</p>
    <ol class="progress">${route.steps.map(step => {
      const position = step.state === "confirmed" ? "done" : step.state === "waiting" ? "waiting" : "todo";
      return `<li data-position="${position}" data-state="${html(step.state)}">
        <i class="progress-dot" aria-hidden="true"></i>
        <div><b>${html(step.name)}</b> <span class="progress-state">${stepStates[step.state] ?? "Unclear"}</span>
        <p>${html(step.detail)}</p></div></li>`;
    }).join("")}</ol>`;
}

function renderRouteDetails(route, showConfirmation) {
  if (!route) return "";
  const sources = sourceList(route.evidence);
  if (!sources && !showConfirmation) return "";
  return `<details class="detail-block shipment-details" data-detail="route checks"><summary>Sources${showConfirmation ? " and scenario" : ""}</summary>
    ${route.evidence?.length ? `<ul>${route.evidence.map(entry => `<li><strong>${html(entry.label)}</strong><br>${html(entry.detail)}</li>`).join("")}</ul>` : ""}
    ${showConfirmation ? `<div class="handoff"><label><input id="assume-confirmed" type="checkbox" ${assumeConfirmation ? "checked" : ""}> Assume supplier confirms the order</label>
      <a href="${negotiator}" target="_blank" rel="noreferrer">Order negotiator</a>
      <p>${assumeConfirmation ? "The scenario assumes supplier confirmation. Transport and receipt still need evidence." : "The scenario leaves supplier confirmation open."}</p></div>` : ""}
    </details>`;
}

function renderTransportMap() {
  const route = activeRoute();
  const target = $("transport-map");
  if (target && route?.tracking && mapLand !== undefined) {
    target.innerHTML = mapFigure(route.tracking, mapLand, route.id, route.name);
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

function renderChaserCases(part) {
  const cases = chaserCases.filter(item => item.material === part.id && part.purchaseOrders?.some(order =>
    order.supplierId === item.supplier_id && order.poNumber === item.po_number));
  if (!cases.length) return "";
  const labels = {open: "Asking", waiting: "Waiting for reply", answered: "Reply received", escalated: "Escalated"};
  const tones = {waiting: "waiting", answered: "ready", escalated: "alert"};
  return `<section class="chaser-cases" aria-labelledby="chaser-heading"><h3 id="chaser-heading">Order confirmations</h3>
    <ul>${cases.map(item => `<li class="next-step-line"><span class="state-chip" data-tone="${tones[item.status] ?? "active"}">${labels[item.status] ?? "Asking"}</span>
      <span>${html(item.supplier_name)}, order ${html(item.po_number)}${item.last_reply_summary ? `: ${html(item.last_reply_summary)}` : item.question ? `: ${html(item.question.split(/(?<=[.?!])\s/)[0])}` : ""}</span></li>`).join("")}</ul></section>`;
}

function renderInspector(force = false) {
  const inspector = $("inspector");
  const part = activePart();
  inspector.hidden = !part;
  if (!part) return;
  const route = activeRoute();
  const routes = routesFor(part);
  const chaser = renderChaserCases(part);
  const key = JSON.stringify([part, routes, route?.id, assumeConfirmation, chaser]);
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
  const stockText = part.status === "verify" ? "Part record needs checking" : `${number(part.usable)} usable in ${siteName()}, ${number(part.required)} needed`;
  const partNote = parent ? `Fitted into the ${nameOf(parent)} by ${part.includedIn.supplier}.` : stockText;
  const wasOpen = new Set([...inspector.querySelectorAll("details[open]")].map(node => node.dataset.detail));
  inspector.innerHTML = `<div class="inspector-head"><div class="inspector-banner">${partArt(part)}
      <div><h2 id="part-heading" tabindex="-1">${html(nameOf(part))}</h2>
      <p>${html(partNote)}</p>${renderEconomics(parent ?? part, route)}</div>
      <button class="text-button" id="close-part" type="button">Close trace</button></div>
    <div class="shipment-head"><div class="shipment-summary"><h3>Where it stands</h3>
      ${renderProgress(route, line)}</div>
      ${renderRouteDetails(route, showConfirmation)}</div>
    </div>
    ${chaser}
    ${route?.tracking ? `<div id="transport-map" class="transport-view" aria-label="Transport map">${mapLand !== undefined ? mapFigure(route.tracking, mapLand, route.id, route.name) : '<p class="side-note">Loading the shipment map.</p>'}</div>` : ""}
    ${parent ? sourceList([part.includedIn.evidence], "Supplier fitting record") : ""}
    <div class="info-grid"><div>
    ${part.adjustments.length ? `<details class="detail-block" data-detail="inventory checks"><summary>Inventory checks</summary><ul>${part.adjustments.map(adjustment =>
      `<li>${html(adjustment.detail)}${adjustment.evidence.length ? `<br><small>${adjustment.evidence.map(html).join(", ")}</small>` : ""}</li>`).join("")}</ul></details>` : ""}</div>
    <div>${parent ? `<p class="side-note">${html(siteName())} receives and counts this part as part of the ${html(nameOf(parent))}.</p>` :
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

async function refreshChaser() {
  if (chaserRequestOpen) return;
  chaserRequestOpen = true;
  let status = "chaser offline";
  let cases = [];
  try {
    const response = await fetch("/api/chaser", {cache: "no-store"});
    if (response.ok) ({status, cases} = await response.json());
  } catch {
    // Dockside remains usable if Supplier Chaser is unavailable.
  } finally {
    chaserRequestOpen = false;
  }
  chaserCases = cases.filter(item => item.status !== "closed");
  const count = chaserCases.length;
  const suppliers = new Set(chaserCases.map(item => item.supplier_id)).size;
  const label = status !== "ok" ? "" : count ? `Chasing ${number(suppliers)} ${suppliers === 1 ? "supplier" : "suppliers"} for ${number(count)} unconfirmed ${count === 1 ? "order line" : "order lines"}.` : "";
  if ($("chaser-count").textContent !== label) $("chaser-count").textContent = label;
  if (state) renderInspector();
}

async function refresh() {
  if (requestOpen) return;
  requestOpen = true;
  void refreshChaser();
  try {
    const [response] = await Promise.all([fetch("/api/dispatch", {cache: "no-store"}), refreshCalls()]);
    if (!response.ok) throw new Error(response.status);
    state = await response.json();
    $("sync").textContent = state.checkedAt ? `Checked ${time.format(new Date(state.checkedAt))}` : "Loading portal data";
    updateOverview();
    renderInspector();
    if (activeRoute()?.tracking) loadMap();
    renderOperations(state);
    renderNextSteps(state);
    window.PartDependencyGraph.prefetch(state.components.map(component => component.id));
  } catch {
    $("sync").textContent = state ? "Refresh failed" : "Disconnected";
    if (!state) $("initial").textContent = "Can’t load the assembly plan. Check the server or refresh this page.";
  } finally {
    requestOpen = false;
  }
}

$("next-strip").addEventListener("click", event => {
  const part = event.target.closest("[data-next-part]");
  if (part) selectPart(part.dataset.nextPart, true);
});
$("stakes").addEventListener("click", event => {
  const part = event.target.closest("[data-stake-part]");
  if (part) selectPart(part.dataset.stakePart);
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
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && selection && $("inspector").contains(document.activeElement)) selectPart(null);
});
new ResizeObserver(drawLinks).observe($("assembly"));
initOperations(refresh);
refresh();
refreshBusy();
setInterval(refresh, 15000);
setInterval(refreshBusy, 5000);
