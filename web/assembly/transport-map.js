// Natural Earth 1:110m land polygons are shared with the carrier plausibility check.
const trackDate = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Berlin", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZoneName: "short",
});
const trackDay = new Intl.DateTimeFormat("en-GB", {timeZone: "Europe/Berlin", day: "numeric", month: "short"});
const trackDistance = new Intl.NumberFormat("en-GB", {maximumFractionDigits: 0});
const trackWidth = 600;
const trackHeight = 315;
const seaLandmarks = [
  {name: "Port Said", lon: 32.31, lat: 31.27}, {name: "Gibraltar", lon: -5.35, lat: 36.14},
  {name: "Dover", lon: 1.31, lat: 51.13}, {name: "Singapore", lon: 103.86, lat: 1.17},
];
let trackDetails;
let trackRequestOpen = false;
let trackRefreshAfter = 0;

async function loadTrackingDetails(onUpdate, retryUntil = Date.now() + 10000) {
  if (trackRequestOpen || Date.now() < trackRefreshAfter) return;
  trackRequestOpen = true;
  try {
    const response = await fetch("/api/tracking", {cache: "no-store", signal: AbortSignal.timeout(3000)});
    if (!response.ok) throw new Error(response.status);
    trackDetails = (await response.json()).shipments;
  } catch {
    trackDetails ??= {};
  } finally {
    trackRequestOpen = false;
    const pending = Object.values(trackDetails ?? {}).some(({flight_match, public_context}) =>
      flight_match?.status === "pending" || flight_match?.position_status === "pending" ||
      public_context?.status === "pending");
    if (pending && Date.now() < retryUntil) {
      trackRefreshAfter = Date.now() + 900;
      setTimeout(() => loadTrackingDetails(onUpdate, retryUntil), 900);
    } else {
      trackRefreshAfter = Date.now() + 60000;
    }
    onUpdate();
  }
}

function mapTime(timestamp) {
  if (!Number.isFinite(Date.parse(timestamp))) return "";
  return `<time datetime="${html(timestamp)}">${html(trackDate.format(new Date(timestamp)))}</time>`;
}

function mapDistance(a, b) {
  const radians = Math.PI / 180;
  const haversine = Math.sin((a.lat - b.lat) * radians / 2) ** 2 +
    Math.cos(a.lat * radians) * Math.cos(b.lat * radians) * Math.sin((a.lon - b.lon) * radians / 2) ** 2;
  return 12742 * Math.asin(Math.min(1, Math.sqrt(haversine)));
}

function mapLocation(position, stops, mode) {
  if (!position) return "";
  const landmarks = mode === "sea" ? [...stops, ...seaLandmarks] : stops;
  const [nearest] = landmarks.map(place => [mapDistance(position, place), place.name]).sort(([a], [b]) => a - b);
  if (nearest && nearest[0] <= 180) return `${nearest[0] < 5 ? "at" : "near"} ${nearest[1]}`;
  const {lat, lon} = position;
  if (mode === "air" && lat > 38 && lat < 62 && lon > -50 && lon < -14) return "over the North Atlantic";
  if (mode === "sea" && lat > 35 && lat < 42 && lon > -5.5 && lon < 11) return "in the western Mediterranean";
  return `at ${Math.abs(lat).toFixed(1)}° ${lat < 0 ? "S" : "N"}, ${Math.abs(lon).toFixed(1)}° ${lon < 0 ? "W" : "E"}`;
}

function mapObservation(position) {
  const name = position.callsign ?? position.name;
  const source = position.simulated && name ? position.source.toLowerCase() :
    position.simulated ? "Simulated carrier report" : position.source;
  const time = mapTime(position.timestamp);
  return `${name ? `${html(name)} from ` : ""}${html(source)}${time ? `, ${time}` : ""}`;
}

function mapFlightCaption(flight, primary) {
  if (!flight) return "";
  const fallback = primary ? ", showing schedule estimate" : "";
  if (flight.status === "pending" || flight.position_status === "pending") {
    return "<p>Checking live flight data.</p>";
  }
  if (flight.status === "ambiguous") {
    const list = flight.candidates.map(candidate => candidate.position ? mapObservation(candidate.position) :
      `${html(candidate.callsign)}, departed ${mapTime(candidate.departed_at)}`).join(". ");
    const unavailable = flight.position_status === "unavailable" ?
      `<p>Live flight positions unavailable${fallback}.</p>` : "";
    return `<p>${flight.candidate_count} possible flights, asking the carrier for the flight number${flight.simulated ? " (simulated)" : ""}.</p>
      ${list ? `<p>${list}.</p>` : ""}${unavailable}`;
  }
  if (flight.status === "matched" && flight.candidates[0].position) {
    return `<p>${mapObservation(flight.candidates[0].position)}.</p>`;
  }
  if (flight.source_status === "unavailable" || flight.status === "matched") {
    return `<p>Live flight data unavailable${fallback}.</p>`;
  }
  return `<p>No matching flight${fallback}.</p>`;
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

function mapFigure(tracking, land, id) {
  const details = trackDetails?.[id];
  if (!details?.route.length) {
    const message = trackDetails ? "Tracking map unavailable for this shipment." : "Loading the shipment map.";
    return `<p class="side-note">${message}</p>`;
  }

  const {route, stops, expected, reported, transfer, flight_match: flight, public_context: context} = details;
  const carrierUnconfirmed = tracking.hold || tracking.status === "booked";
  const useEstimate = details.current_position?.source === "Carrier feed" &&
    (carrierUnconfirmed || (tracking.stale && tracking.status !== "arrived_port"));
  const primary = useEstimate ? expected ?? reported : details.current_position;
  const reportedDistance = primary && reported ? mapDistance(primary, reported) : 0;
  const compareReport = tracking.mode !== "air" && !carrierUnconfirmed && reportedDistance > 25;
  const candidates = flight?.status === "ambiguous" ? flight.candidates
    .map(candidate => candidate.position).filter(position => position && (!primary || mapDistance(primary, position) > 25)) : [];
  const xy = ({lon, lat}) => [lon, lat];
  const alternatives = [...(compareReport ? [reported] : []), ...candidates];
  const extra = [...transfer, ...[primary, ...alternatives].filter(Boolean).map(xy)];
  const {project, coast, grid} = mapProjection(route, extra);
  const polyline = points => points.map(point => project(point).join(",")).join(" ");

  const plottedStops = stops.map((stop, index) => {
    const [x, y] = project(xy(stop));
    const anchor = x < trackWidth / 3 ? "start" : x > trackWidth * 2 / 3 ? "end" : "middle";
    const lowerLabel = (transfer.length && index === stops.length - 1) || (index === 1 && stops.length === 3);
    return `<circle class="map-stop" cx="${x}" cy="${y}" r="4"/>
      <text x="${x}" y="${y + (lowerLabel ? 23 : -14)}" text-anchor="${anchor}">${html(stop.name)}</text>`;
  }).join("");
  const handoff = transfer.length ? `<polyline class="map-transfer" points="${polyline(transfer)}"/>` : "";
  const ghosts = alternatives.map(position => {
    const [x, y] = project(xy(position));
    return `<circle class="map-ghost ${position === reported ? "carrier-ghost" : "flight-ghost"}" cx="${x}" cy="${y}" r="6">
      <title>${html(position.callsign ?? "Carrier report")}</title></circle>`;
  }).join("");

  let marker = "";
  if (primary) {
    const [x, y] = project(xy(primary));
    marker = `<circle class="map-pulse" cx="${x}" cy="${y}" r="8"/>
      <circle class="map-position" cx="${x}" cy="${y}" r="7"><title>${html(primary.callsign ?? primary.source)}</title></circle>`;
  }

  const estimated = primary?.source === "Schedule estimate";
  const location = primary?.label ? primary.label[0].toLowerCase() + primary.label.slice(1) : mapLocation(primary, stops, tracking.mode);
  const mode = {air: "air freight", sea: "sea freight", road: "road freight"}[tracking.mode] ?? "shipment";
  const label = details.cargo ? `${details.cargo} ${mode}` : mode[0].toUpperCase() + mode.slice(1);
  const arrival = Number.isFinite(Date.parse(details.eta)) ? `, due ${trackDay.format(new Date(details.eta))}` : "";
  const positionLabel = primary?.simulated ? "simulated" : estimated ? "estimate" : "";
  const heading = `${label}${arrival}${location ? `, ${location}` : ""}${positionLabel ? ` (${positionLabel})` : ""}`;
  const progress = estimated ? [...route.slice(0, expected.segment + 1), xy(expected)] : [];

  const reportNote = compareReport ? `<p>${mapObservation(reported)}. ${reported.plausibility === "implausible" ?
    "Reported position falls inland." : `${trackDistance.format(reportedDistance)} km from the current marker.`}</p>` : "";
  const vesselNote = context?.status === "pending" ? "<p>Checking live vessel data.</p>" :
    ["unavailable", "no_position"].includes(context?.status) ? `<p>Live vessel data unavailable${estimated ? ", showing schedule estimate" : ""}.</p>` : "";
  const carrierNote = !flight && primary && !estimated ? `<p>${mapObservation(primary)}.</p>` : "";

  return `<figure class="shipment-map">
    <h4 class="map-heading">${html(heading)}</h4>
    <svg viewBox="0 0 ${trackWidth} ${trackHeight}" role="img" aria-label="${html(stops.map(stop => stop.name).join(" to "))}">
      <path class="map-grid" d="${grid}"/>
      <path class="map-land" fill-rule="evenodd" d="${coast(land ?? [])}"/>
      <polyline class="map-route" points="${polyline(route)}"/>
      ${progress.length ? `<polyline class="map-progress" points="${polyline(progress)}"/>` : ""}
      ${handoff}${plottedStops}${ghosts}${marker}
    </svg>
    <figcaption>
      <div class="map-legend">
        ${primary ? `<span><i class="map-key position-key" aria-hidden="true"></i><strong>${estimated ? "Estimate" : "Position"}</strong></span>` : ""}
        ${compareReport ? '<span><i class="map-key ghost-key carrier-ghost" aria-hidden="true"></i>Carrier report</span>' : ""}
        ${candidates.length ? '<span><i class="map-key ghost-key" aria-hidden="true"></i>Possible flight</span>' : ""}
        <span><i class="map-key route-key" aria-hidden="true"></i>Route</span>
        ${transfer.length ? '<span><i class="map-key transfer-key" aria-hidden="true"></i>Road leg</span>' : ""}
      </div>
      ${mapFlightCaption(flight, primary)}${vesselNote}${carrierNote}${reportNote}
    </figcaption>
  </figure>`;
}
