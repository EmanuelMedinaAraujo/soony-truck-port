// Natural Earth 1:110m land polygons are shared with the carrier plausibility check.
const trackDate = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Berlin", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZoneName: "short",
});
const trackDistance = new Intl.NumberFormat("en-GB", {maximumFractionDigits: 0});
const trackWidth = 600;
const trackHeight = 315;
let trackDetails;
let trackRequestOpen = false;
let trackRefreshAfter = 0;

async function loadTrackingDetails(onUpdate) {
  if (trackRequestOpen || Date.now() < trackRefreshAfter) return;
  trackRequestOpen = true;
  try {
    const response = await fetch("/api/tracking", {cache: "no-store", signal: AbortSignal.timeout(12000)});
    if (!response.ok) throw new Error(response.status);
    trackDetails = (await response.json()).shipments;
  } catch {
    trackDetails ??= {};
  } finally {
    trackRequestOpen = false;
    trackRefreshAfter = Date.now() + 60000;
    onUpdate();
  }
}

function mapTime(timestamp) {
  if (!Number.isFinite(Date.parse(timestamp))) return "";
  return `<time datetime="${html(timestamp)}">${html(trackDate.format(new Date(timestamp)))}</time>`;
}

function mapPublicCaption(context) {
  if (!context) return "";
  const {source, status, items, timestamp} = context;
  if (items.length) {
    const scope = items[0].kind === "aircraft" ? "Aircraft near Frankfurt for context" : "Matched vessel";
    const label = status === "live" ? "Live public data" : "Last public data";
    return `<p><span class="map-key public-key" aria-hidden="true"></span><strong>${label}</strong> from ${html(source)}.
      ${scope}. ${mapTime(timestamp)}</p>`;
  }
  const messages = {
    not_connected: "AIS vessel tracking is not connected.",
    missing_identity: "AIS vessel tracking needs a vessel identifier.",
    no_position: "AIS Stream has no recent position for this vessel in the area.",
    unavailable: `${source} is temporarily unavailable.`,
    live: "OpenSky Network found no recent aircraft positions near Frankfurt.",
    stale: "OpenSky Network found no recent aircraft positions near Frankfurt.",
  };
  return `<p>${html(messages[status] ?? "")} ${mapTime(timestamp)}</p>`;
}

function mapLongitude(lon, center) {
  return ((lon - center + 540) % 360 + 360) % 360 - 180 + center;
}

function mapProjection(line, extra) {
  let previousLon = line[0][0];
  const unwrapped = line.map(([lon, lat]) => {
    previousLon = mapLongitude(lon, previousLon);
    return [previousLon, lat];
  });
  const lons = unwrapped.map(point => point[0]);
  const relativeTo = (Math.min(...lons) + Math.max(...lons)) / 2;
  const frame = [...unwrapped, ...extra.map(([lon, lat]) => [mapLongitude(lon, relativeTo), lat])];
  const minLat = Math.min(...frame.map(point => point[1]));
  const maxLat = Math.max(...frame.map(point => point[1]));
  const minLon = Math.min(...frame.map(point => point[0]));
  const maxLon = Math.max(...frame.map(point => point[0]));
  const latCenter = (minLat + maxLat) / 2;
  const lonCenter = (minLon + maxLon) / 2;
  const stretch = Math.max(0.35, Math.cos(latCenter * Math.PI / 180));
  const scale = Math.min((trackWidth - 106) / Math.max(1.75, (maxLon - minLon) * stretch),
    (trackHeight - 88) / Math.max(1.4, maxLat - minLat));
  const rawProject = (lon, lat) => [
    Math.round((trackWidth / 2 + (lon - lonCenter) * scale * stretch) * 10) / 10,
    Math.round((trackHeight / 2 - (lat - latCenter) * scale) * 10) / 10,
  ];
  const project = ([lon, lat]) => rawProject(mapLongitude(lon, relativeTo), lat);

  const lonRadius = trackWidth / (2 * scale * stretch);
  const latRadius = trackHeight / (2 * scale);
  const gridStep = [0.25, 0.5, 1, 2, 5, 10, 20, 30, 45, 90].find(step => step * scale >= 32) ?? 90;
  const grid = [];
  for (let lon = Math.ceil((lonCenter - lonRadius) / gridStep) * gridStep; lon <= lonCenter + lonRadius; lon += gridStep) {
    const [x] = rawProject(lon, latCenter);
    grid.push(`M${x} 0V${trackHeight}`);
  }
  for (let lat = Math.ceil((latCenter - latRadius) / gridStep) * gridStep; lat <= latCenter + latRadius; lat += gridStep) {
    const [, y] = rawProject(lonCenter, lat);
    grid.push(`M0 ${y}H${trackWidth}`);
  }
  const coast = land => land.flatMap(ring => {
    const ringLons = ring.map(point => point[0]);
    const low = Math.min(...ringLons);
    const high = Math.max(...ringLons);
    const first = Math.ceil((lonCenter - lonRadius - high) / 360);
    const last = Math.floor((lonCenter + lonRadius - low) / 360);
    const paths = [];
    for (let shift = first; shift <= last; shift++) {
      paths.push(`M${ring.map(([lon, lat]) => rawProject(lon + shift * 360, lat).join(",")).join("L")}Z`);
    }
    return paths;
  }).join("");
  return {project, coast, grid: grid.join("")};
}

function frankfurtInset(stops, items, projectedRoute) {
  if (!items.length || items[0].kind !== "aircraft") return "";
  const airport = stops.at(-1);
  const width = 154;
  const height = 96;
  const corners = [[14, trackHeight - height - 14], [trackWidth - width - 14, trackHeight - height - 14],
    [14, 14], [trackWidth - width - 14, 14]];
  const clearance = ([x, y]) => Math.min(...projectedRoute.map(([px, py]) => Math.hypot(
    Math.max(x - px, 0, px - x - width), Math.max(y - py, 0, py - y - height),
  )));
  const [left, top] = corners.reduce((best, next) => clearance(next) > clearance(best) ? next : best);
  const scale = Math.min(28 / Math.max(0.2, ...items.map(point => Math.abs(point.lat - airport.lat))),
    57 / Math.max(0.2, ...items.map(point => Math.abs(point.lon - airport.lon))));
  const dots = items.map(item => `<circle class="map-public" cx="${width / 2 + (item.lon - airport.lon) * scale}"
    cy="${58 - (item.lat - airport.lat) * scale}" r="2.8"><title>${html(item.name)}</title></circle>`).join("");
  return `<g class="map-inset" transform="translate(${left} ${top})">
    <rect class="map-inset-background" width="${width}" height="${height}" rx="5"/>
    <path class="map-inset-grid" d="M12 58H142 M77 31V87"/>
    ${dots}<circle class="map-stop" cx="77" cy="58" r="3.5"/>
    <text x="10" y="19">Near Frankfurt</text>
  </g>`;
}

function mapFigure(tracking, land, id) {
  const details = trackDetails?.[id];
  if (!details?.route.length) {
    const message = trackDetails ? "Tracking comparison is unavailable." : "Loading the shipment map.";
    return `<p class="side-note">${message}</p>`;
  }

  const {route, stops, expected, reported, transfer, public_context: context} = details;
  const carrierUsable = reported && reported.plausibility === "plausible" && !tracking.hold;
  const primary = carrierUsable ? reported : expected;
  const confirmedDeparture = tracking.status !== "booked" && !tracking.hold;
  const differs = confirmedDeparture && expected && reported && details.verdict && details.verdict !== "feeds agree";
  const comparison = differs ? (carrierUsable ? expected : reported) : null;
  const xy = ({lon, lat}) => [lon, lat];
  const extra = [...transfer, ...[primary, comparison].filter(Boolean).map(xy)];
  const {project, coast, grid} = mapProjection(route, extra);
  const polyline = points => points.map(point => project(point).join(",")).join(" ");

  const plottedStops = stops.map((stop, index) => {
    const [x, y] = project(xy(stop));
    const anchor = x < trackWidth / 3 ? "start" : x > trackWidth * 2 / 3 ? "end" : "middle";
    const isTransferStop = transfer.length && index === stops.length - 1;
    const offset = isTransferStop || (index === 1 && stops.length === 3) ? 22 : -14;
    return `<circle class="map-stop" cx="${x}" cy="${y}" r="${offset > 0 ? 3.5 : 4.5}"/>
      <text x="${x}" y="${y + offset}" text-anchor="${anchor}">${html(stop.name)}</text>`;
  }).join("");
  const handoff = transfer.length ? (() => {
    const [x, y] = project(transfer[1]);
    const anchor = x < trackWidth / 2 ? "start" : "end";
    return `<polyline class="map-transfer" points="${polyline(transfer)}"/>
      <circle class="map-stop" cx="${x}" cy="${y}" r="3"/>
      <text x="${x}" y="${y - 12}" text-anchor="${anchor}">Hamburg warehouse</text>`;
  })() : "";

  const traffic = (context?.items ?? []).map(item => {
    const [x, y] = project(xy(item));
    if (x < 6 || x > trackWidth - 6 || y < 6 || y > trackHeight - 6) return "";
    return `<circle class="map-public" cx="${x}" cy="${y}" r="2.7">
      <title>${html(item.name)} from ${html(item.source)}</title></circle>`;
  }).join("");
  const inset = frankfurtInset(stops, context?.items ?? [], [...route, ...extra].map(project));

  let cursor = "";
  let connector = "";
  let distanceLabel = "";
  if (primary) {
    const [x, y] = project(xy(primary));
    if (comparison) {
      const [cx, cy] = project(xy(comparison));
      const separation = Math.hypot(cx - x, cy - y);
      if (separation > 15) {
        const distance = `${trackDistance.format(details.divergence_km)} km apart`;
        const [labelX, labelY] = [(x + cx) / 2, (y + cy) / 2];
        connector = `<line class="map-connector" x1="${x}" y1="${y}" x2="${cx}" y2="${cy}"/>
          <circle class="map-ghost ${carrierUsable ? "schedule-ghost" : "carrier-ghost"}" cx="${cx}" cy="${cy}" r="6">
            <title>${html(comparison.source)}</title></circle>`;
        distanceLabel = `<text class="map-distance" x="${labelX}" y="${labelY + (labelY < 42 ? 21 : -10)}"
          text-anchor="middle">${html(distance)}</text>`;
      }
    }
    const reportOutline = comparison && carrierUsable ?
      `<circle class="map-ghost map-report-outline" cx="${x}" cy="${y}" r="8"/>` : "";
    cursor = `<circle class="map-pulse" cx="${x}" cy="${y}" r="8"/>${reportOutline}
      <circle class="map-position" cx="${x}" cy="${y}" r="${reportOutline ? 5 : 7}"><title>${html(primary.source)}</title></circle>`;
  }

  const progressLine = expected ? [...route.slice(0, expected.segment + 1), xy(expected)] : [];
  const scheduleLabel = expected ? `Schedule estimate ${Number(expected.progress_pct)}%` : "";
  const caption = tracking.hold ? "Supplier hold. Departure is unconfirmed." : tracking.status === "booked" ?
    "Booking on file. Departure is unconfirmed." : tracking.status === "arrived_port" && carrierUsable ?
    `At the ${tracking.terminal} terminal. Waiting for pickup.` : reported?.plausibility === "implausible" ?
    "The carrier position falls inland. The marker shows the schedule estimate." : tracking.stale && carrierUsable ?
    "The carrier report is overdue." : "";
  const distance = differs ? `${trackDistance.format(details.divergence_km)} km apart.` : "";
  const name = [tracking.origin, tracking.terminal].filter(Boolean).join(" to ");
  return `<figure class="shipment-map">
    <svg viewBox="0 0 ${trackWidth} ${trackHeight}" role="img" aria-label="${html(`${name}. ${primary?.source ?? "Scheduled route"}. ${distance} ${caption}`)}">
      <path class="map-grid" d="${grid}"/>
      <path class="map-land" fill-rule="evenodd" d="${coast(land)}"/>
      <polyline class="map-route" points="${polyline(route)}"/>
      ${progressLine.length ? `<polyline class="map-progress" points="${polyline(progressLine)}"/>` : ""}
      ${handoff}${traffic}${connector}${plottedStops}${distanceLabel}${cursor}${inset}
    </svg>
    <figcaption>
      <div class="map-legend">
        ${primary ? `<span><i class="map-key position-key"></i><strong>${carrierUsable ? "Carrier report" : scheduleLabel}</strong></span>` : ""}
        ${comparison ? `<span><i class="map-key ghost-key ${carrierUsable ? "schedule-ghost" : "carrier-ghost"}"></i>
          ${carrierUsable ? scheduleLabel : "Carrier report"}</span>` : ""}
        <span><i class="map-key route-key"></i>Scheduled route</span>
        ${transfer.length ? '<span><i class="map-key transfer-key"></i>Road transfer</span>' : ""}
      </div>
      ${caption || distance || (confirmedDeparture && reported?.timestamp) ?
        `<p>${html(caption)} ${html(distance)} ${confirmedDeparture && reported?.timestamp ?
          `${reported.simulated ? "Simulated carrier update" : "Carrier update"} ${mapTime(reported.timestamp)}.` : ""}</p>` : ""}
      ${mapPublicCaption(context)}
    </figcaption>
  </figure>`;
}
