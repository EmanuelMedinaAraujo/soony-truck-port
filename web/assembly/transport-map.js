// The coastlines come from Natural Earth 1:110m land. Planned links are schematic.
const trackDate = new Intl.DateTimeFormat("en-GB", {timeZone: "Europe/Berlin", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});

function pointInRing({lat, lon}, ring) {
  let inside = false;
  for (let index = 0, last = ring.length - 1; index < ring.length; last = index++) {
    const [ax, ay] = ring[index];
    const [bx, by] = ring[last];
    if ((ay > lat) !== (by > lat) && lon < (bx - ax) * (lat - ay) / (by - ay) + ax) inside = !inside;
  }
  return inside;
}

function distanceKm(a, b) {
  const radians = degrees => degrees * Math.PI / 180;
  const halfDistance = Math.sin(radians(a.lat - b.lat) / 2) ** 2 +
    Math.cos(radians(a.lat)) * Math.cos(radians(b.lat)) * Math.sin(radians(a.lon - b.lon) / 2) ** 2;
  return 12742 * Math.asin(Math.min(1, Math.sqrt(halfDistance)));
}

function mapFigure(tracking, land) {
  const inland = tracking.mode === "sea" && tracking.position && tracking.status !== "arrived_port" &&
    land.some(ring => pointInRing(tracking.position, ring)) && tracking.stops.every(stop => distanceKm(stop, tracking.position) > 65);
  const position = inland ? null : tracking.position;
  const atPort = tracking.status === "arrived_port" && position;
  const stops = atPort ? [{name: tracking.terminal, ...position}] : tracking.stops;
  const points = [...stops, ...(position ? [position] : [])];
  const updated = tracking.updatedAt && Number.isFinite(Date.parse(tracking.updatedAt)) ? trackDate.format(new Date(tracking.updatedAt)) : null;
  const caption = tracking.hold ? "Supplier hold. Ask the carrier before relying on the route." : inland ?
    "The reported vessel position falls inland. Carrier location needs a check." : tracking.status === "arrived_port" && position ?
    `Last confirmed at ${tracking.terminal}. Waiting for pickup.` : position ?
    tracking.stale ? "Last reported position. The carrier update is overdue." : "Last reported position from the carrier feed." :
    tracking.status === "booked" ? "Booking on file. Carrier position is unavailable." : "Carrier position is unavailable.";

  if (!points.length) return `<p class="side-note">${html(caption)}</p>`;
  const lons = points.map(point => point.lon), lats = points.map(point => point.lat);
  const width = 620, height = 245;
  const minLat = Math.min(...lats), maxLat = Math.max(...lats), minLon = Math.min(...lons), maxLon = Math.max(...lons);
  const latCenter = (minLat + maxLat) / 2, lonCenter = (minLon + maxLon) / 2;
  const stretch = Math.max(.32, Math.cos(latCenter * Math.PI / 180));
  const scale = Math.min((width - 96) / Math.max(8, (maxLon - minLon) * stretch), (height - 72) / Math.max(4, maxLat - minLat));
  const project = (lon, lat) => [(width / 2 + (lon - lonCenter) * scale * stretch).toFixed(1), (height / 2 - (lat - latCenter) * scale).toFixed(1)];
  const landPath = land.map(ring => `M${ring.map(([lon, lat]) => project(lon, lat).join(",")).join("L")}Z`).join("");
  const route = stops.map(({lon, lat}) => project(lon, lat).join(",")).join(" ");
  const routePoints = stops.map(({lon, lat, name}, index) => {
    const [x, y] = project(lon, lat).map(Number);
    const offset = index % 2 ? 22 : -12;
    const anchor = x < width / 3 ? "start" : x > width * 2 / 3 ? "end" : "middle";
    return `<circle class="map-stop" cx="${x}" cy="${y}" r="4"/><text x="${x}" y="${y + offset}" text-anchor="${anchor}">${html(name)}</text>`;
  }).join("");
  const marker = position ? (() => {
    const [x,y] = project(position.lon, position.lat);
    return `<circle class="map-position${tracking.stale ? ' stale' : ''}" cx="${x}" cy="${y}" r="7"/>`;
  })() : "";
  const routeName = [tracking.origin, tracking.terminal].filter(Boolean).join(" to ");
  return `<figure class="shipment-map"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${html(`Shipment ${tracking.reference} from ${routeName}. ${caption}`)}">
      <path class="map-land" fill-rule="evenodd" d="${landPath}"/>
      <polyline class="map-route" points="${route}"/>${routePoints}${marker}
    </svg><figcaption><div>${atPort ? `From ${html(tracking.origin)}` : `<span class="map-key route-key"></span> Schematic route${marker ? '<span class="map-key position-key"></span> Reported location' : ""}`}</div>
      <p>${html(caption)}${updated ? ` <time datetime="${html(tracking.updatedAt)}">Tracker report ${html(updated)}.</time>` : ""}</p></figcaption></figure>`;
}
