"""Carrier reports, schedule estimates, and shipment-specific flight and vessel tracking."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core import rules
from core.dispatch import coordinates

Point = tuple[float, float]  # longitude, latitude (GeoJSON order)
SINGAPORE = {"name": "Singapore", "lat": 1.1714, "lon": 103.8611}
AGREEMENT_KM = 25
PUBLIC_CACHE_SECONDS = 240
PUBLIC_TIMEOUT = 3
OPEN_SKY = "OpenSky Network"
AIS_STREAM = "AIS Stream"
SIMULATED_FLIGHTS = "Simulated flight data"
AIRPORT_ICAO = {"ICN": "RKSI", "ORD": "KORD", "FRA": "EDDF", "BOM": "VABB", "CAN": "ZGGG"}
DEPARTURE_WINDOW = 12 * 3600
MAX_FLIGHT_CANDIDATES = 3
CARGO_CARRIERS = {
    "lufthansa cargo": ("GEC",),
    "atlas air": ("GTI",),
    "cargolux": ("CLX",),
    "korean air cargo": ("KAL",),
    "air india cargo": ("AIC",),
    "china southern cargo": ("CSN",),
}
CARGO_PREFIXES = tuple(prefix for prefixes in CARGO_CARRIERS.values() for prefix in prefixes)

# Callsign, hours from the shipment ETD, flight duration, and fraction at the
# last simulated position report. The forwarder in the demo uses several airlines.
MOCK_FLIGHTS = {
    ("RKSI", "EDDF"): (("KAL529", 1, 13, 0.44), ("GEC8471", -3, 13, 0.77)),
    ("KORD", "EDDF"): (("GEC8221", 2, 9, 0.62),),
    ("VABB", "EDDF"): (("GEC7571", 2, 9, 0.60),),
    ("ZGGG", "EDDF"): (("CSN465", 3, 13, 0.65),),
}


def point_of(place: dict) -> Point:
    return place["lon"], place["lat"]


def distance_km(a: Point, b: Point) -> float:
    lon_a, lat_a, lon_b, lat_b = map(math.radians, (*a, *b))
    h = math.sin((lat_b - lat_a) / 2) ** 2 + (
        math.cos(lat_a) * math.cos(lat_b) * math.sin((lon_b - lon_a) / 2) ** 2
    )
    return 12742 * math.asin(min(1, math.sqrt(h)))


def great_circle(a: Point, b: Point) -> tuple[Point, ...]:
    """Sample the short spherical arc at intervals of at most 150 km."""
    angle = distance_km(a, b) / 6371
    if angle == 0:
        return a, b
    lon, lat = map(math.radians, a)
    target_lon, target_lat = map(math.radians, b)
    heading = math.atan2(
        math.sin(target_lon - lon) * math.cos(target_lat),
        math.cos(lat) * math.sin(target_lat)
        - math.sin(lat) * math.cos(target_lat) * math.cos(target_lon - lon),
    )
    steps = math.ceil(angle * 6371 / 150)
    points = [a]
    for step in range(1, steps):
        traveled = angle * step / steps
        phi = math.asin(
            math.sin(lat) * math.cos(traveled) + math.cos(lat) * math.sin(traveled) * math.cos(heading)
        )
        lam = lon + math.atan2(
            math.sin(heading) * math.sin(traveled) * math.cos(lat),
            math.cos(traveled) - math.sin(lat) * math.sin(phi),
        )
        points.append(((math.degrees(lam) + 180) % 360 - 180, math.degrees(phi)))
    return (*points, b)


# For the lanes in the shipment book, in longitude/latitude order.
EAST_ASIA = ((129.20, 35), (127, 33.8), (124, 32), (122, 26), (120, 23.5))
SOUTH_CHINA_SEA = ((114.44, 22.31), (114.22, 21.93), (111.48, 12.52), (104.48, 1.34))
MALACCA = ((103.60, 1.10), (102, 2), (100.60, 3.20), (98.13, 5.81), (94, 6.5), (80, 5.8), (73, 6))
SUEZ = (
    (51.5, 11.5), (46, 12), (43.3, 12.55), (42, 15), (39, 19),
    (37, 23), (35.5, 25.6), (34.25, 27.8), (32.6, 29.7), (32.56, 30.21),
    (32.38, 30.32), (32.31, 31.10), (32.36, 31.34),
)
EUROPE = (
    (29, 32.8), (24, 34), (18, 35.5), (15, 36), (12, 37.25), (9, 38),
    (5, 38), (-0.2, 36.8), (-5.6, 35.95), (-9.7, 37.5), (-10, 43.7),
    (-6, 47.5), (-5.3, 49), (-3, 49.6), (0.5, 50.3), (1.5, 50.9),
    (2.4, 51.3), (3.9, 52.7),
)
ELBE = ((4.9, 53.5), (6.38, 53.69), (8.25, 53.9), (9.04, 53.87),
        (9.40, 53.78), (9.54, 53.63), (9.75, 53.55))


def sea_waypoints(a: Point, b: Point) -> tuple[Point, ...]:
    origin_lon, _ = a
    destination_lon, _ = b
    east_asia = EAST_ASIA if origin_lon > 120 else ()
    shenzhen = SOUTH_CHINA_SEA if origin_lon > 110 else ()
    if destination_lon > 100:  # Asian feeder leg to Singapore
        return a, *east_asia, *shenzhen, b
    departure = MALACCA if origin_lon > 100 else ((72.4, 19), (66.2, 17), (60, 14.5))
    arrival = ELBE if destination_lon > 8 else ()
    return a, *departure, *SUEZ, *EUROPE, *arrival, b


@lru_cache(maxsize=32)
def sea_leg(a: Point, b: Point) -> tuple[tuple[Point, ...], str]:
    try:
        from searoute import searoute
    except ImportError:
        return sea_waypoints(a, b), "Maritime waypoints"
    # Keep the graph out of polar passages and join its nearest nodes to the ports.
    feature = searoute(a, b, restrictions=["northwest", "northeast"])
    points = tuple(tuple(point) for point in feature.geometry.coordinates)
    if len(points) > 2:
        start = () if a == points[0] else (a,)
        end = () if b == points[-1] else (b,)
        return (*start, *points, *end), "Searoute"
    return sea_waypoints(a, b), "Maritime waypoints"


def route_geometry(mode: str, stops: list[dict]) -> tuple[tuple[Point, ...], str]:
    line: tuple[Point, ...] = ()
    source = ""
    for start, end in zip(stops, stops[1:]):
        a, b = point_of(start), point_of(end)
        if mode == "sea":
            leg, leg_source = sea_leg(a, b)
            source = "Maritime waypoints" if source == "Maritime waypoints" else leg_source
        elif mode == "air":
            leg, source = great_circle(a, b), "Great-circle flight path"
        else:
            leg, source = (a, b), "Direct road estimate"
        line += leg[1:] if line else leg
    return line, source


def schedule_progress(now: dt.datetime, etd: str | None, eta: str | None) -> float | None:
    try:
        departed, arrives = rules.ts(etd), rules.ts(eta)
    except ValueError:
        return None
    if not departed or not arrives or arrives <= departed:
        return None
    now = now if now.tzinfo else now.replace(tzinfo=rules.timezone())
    return min(1, max(0, (now - departed) / (arrives - departed)))


def position_along(line: tuple[Point, ...], progress: float) -> tuple[Point, int]:
    if progress <= 0:
        return line[0], 0
    if progress >= 1:
        return line[-1], len(line) - 2
    lengths = [distance_km(a, b) for a, b in zip(line, line[1:])]
    remaining = sum(lengths) * progress
    for index, ((lon, lat), (next_lon, next_lat), length) in enumerate(zip(line, line[1:], lengths)):
        if length and remaining < length:
            fraction = remaining / length
            delta_lon = (next_lon - lon + 180) % 360 - 180
            return ((lon + delta_lon * fraction + 180) % 360 - 180, lat + (next_lat - lat) * fraction), index
        remaining -= length
    return line[-1], len(line) - 2


@lru_cache(maxsize=1)
def land_rings() -> list:
    # Shared with the map. Natural Earth 1:110m land polygons.
    return json.loads((rules.ROOT / "web" / "assembly" / "map-land.json").read_text(encoding="utf-8"))


def is_on_land(point: Point) -> bool:
    lon, lat = point
    for ring in land_rings():
        inside = False
        previous_lon, previous_lat = ring[-1]
        for edge_lon, edge_lat in ring:
            if (edge_lat > lat) != (previous_lat > lat):
                crossing_lon = edge_lon + (previous_lon - edge_lon) * (lat - edge_lat) / (previous_lat - edge_lat)
                if lon < crossing_lon:
                    inside = not inside
            previous_lon, previous_lat = edge_lon, edge_lat
        if inside:
            return True
    return False


def reported_position(shipment: dict, stops: list[dict]) -> dict | None:
    position = coordinates(shipment["last_lat"], shipment["last_lon"])
    if not position:
        return None
    point = point_of(position)
    # A coarse coastline does not resolve the approach to many berths. The
    # The terminal and truck pickup also share the port's ERP coordinates.
    sea = shipment["mode"] == "sea"
    inland = sea and is_on_land(point) and all(distance_km(point, point_of(stop)) > 20 for stop in stops)
    plausibility = "implausible" if inland else "unconfirmed" if shipment["status"] == "booked" else "plausible"
    return {**position, "source": "Carrier feed", "timestamp": shipment["updated_at"],
            "plausibility": plausibility, "simulated": sea}


public_lock = Lock()
public_cache: dict[tuple, tuple[float, dict]] = {}
public_pending: dict[tuple, Future] = {}
public_workers = ThreadPoolExecutor(max_workers=6, thread_name_prefix="tracking")
token_lock = Lock()
opensky_token = ("", 0.0)


def public_context(source: str, status: str, items: list | None = None, timestamp: str | None = None) -> dict:
    return {"source": source, "status": status, "timestamp": timestamp, "items": items or []}


def cached_public(key: tuple, fetch, *, background: bool) -> dict:
    def refresh() -> dict:
        try:
            result = fetch()
            with public_lock:
                retry = result.pop("retry_after", 0)
                public_cache[key] = time.monotonic() + max(PUBLIC_CACHE_SECONDS, retry), result
            return result
        finally:
            with public_lock:
                public_pending.pop(key, None)

    with public_lock:
        expires, cached = public_cache.get(key, (0, None))
        if time.monotonic() < expires:
            return cached
        future = public_pending.get(key)
        if future is None:
            future = public_workers.submit(refresh)
            public_pending[key] = future
    return public_context(key[0], "pending") if background else future.result()


def utc_iso(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.UTC).isoformat(timespec="seconds")


def opensky_request(path: str, query: dict) -> dict:
    try:
        url = f"https://opensky-network.org/api/{path}?{urlencode(query, doseq=True)}"
        token = opensky_access_token()
        request = Request(url, headers={"Authorization": f"Bearer {token}"}) if token else url
        with urlopen(request, timeout=PUBLIC_TIMEOUT) as response:
            data = json.load(response)
        if path == "flights/departure":
            if not isinstance(data, list):
                return {"status": "unavailable"}
            return {"status": "available" if data else "no_data", "data": data}
        if not isinstance(data, dict) or not isinstance(data.get("time"), (int, float)):
            return {"status": "unavailable"}
        states = data.get("states")
        if states is not None and not isinstance(states, list):
            return {"status": "unavailable"}
        return {"status": "available" if states else "no_data", "data": data}
    except HTTPError as error:
        try:
            delay = (error.headers or {}).get("X-Rate-Limit-Retry-After-Seconds", "")
            return {"status": "no_data" if error.code == 404 else "unavailable",
                    "retry_after": int(delay) if delay.isdecimal() else 0}
        finally:
            error.close()
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return {"status": "unavailable"}


def opensky_access_token() -> str:
    global opensky_token
    client_id, client_secret = os.getenv("OPENSKY_CLIENT_ID"), os.getenv("OPENSKY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return ""
    with token_lock:
        token, expires = opensky_token
        if time.monotonic() < expires:
            return token
        form = urlencode({"grant_type": "client_credentials", "client_id": client_id,
                          "client_secret": client_secret}).encode()
        url = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
        with urlopen(Request(url, data=form), timeout=PUBLIC_TIMEOUT) as response:
            payload = json.load(response)
        token = payload["access_token"]
        opensky_token = token, time.monotonic() + max(0, float(payload["expires_in"]) - 60)
        return token


def mock_departures(origin: str, destination: str, etd: int, now: float, line: tuple[Point, ...]) -> list[dict]:
    departures = []
    for index, (callsign, offset, duration, fraction) in enumerate(MOCK_FLIGHTS.get((origin, destination), ())):
        departure = etd + offset * 3600
        if departure > now:
            continue
        observed = min(now, departure + duration * 3600 * fraction)
        position = None
        if line:
            (lon, lat), _ = position_along(line, (observed - departure) / (duration * 3600))
            position = {"lat": lat, "lon": lon, "timestamp": utc_iso(observed),
                        "source": SIMULATED_FLIGHTS, "simulated": True}
        departures.append({
            "icao24": f"mock-{index + 1}", "callsign": callsign, "firstSeen": departure,
            "estDepartureAirport": origin, "estArrivalAirport": destination, "position": position,
        })
    return departures


def ranked_departures(rows: list, origin: str, destination: str, etd: int, prefixes: tuple) -> list[dict]:
    candidates = {}
    for row in rows:
        if not isinstance(row, dict) or (row.get("estDepartureAirport"), row.get("estArrivalAirport")) != (origin, destination):
            continue
        departed = row.get("firstSeen")
        if type(departed) is not int or abs(departed - etd) > DEPARTURE_WINDOW:
            continue
        icao24 = row.get("icao24")
        callsign = row.get("callsign")
        if not isinstance(icao24, str) or not icao24.strip():
            continue
        callsign = callsign.strip().upper() if isinstance(callsign, str) else ""
        if prefixes and not callsign.startswith(prefixes):
            continue
        flight = {**row, "icao24": icao24.strip().lower(), "callsign": callsign}
        candidates.setdefault((flight["icao24"], departed), flight)
    return sorted(candidates.values(), key=lambda flight: (
        abs(flight["firstSeen"] - etd), not flight["callsign"].startswith(CARGO_PREFIXES),
        flight["firstSeen"], flight["icao24"],
    ))


def flight_position(flight: dict, data: dict) -> dict | None:
    fetched = data["time"]
    if not math.isfinite(fetched) or not -30 <= time.time() - fetched <= PUBLIC_CACHE_SECONDS:
        return None
    for state in data.get("states") or []:
        if not isinstance(state, list) or len(state) < 9 or state[0] != flight["icao24"]:
            continue
        callsign, observed = state[1], state[3]
        if not isinstance(callsign, str) or not flight["callsign"] or callsign.strip().upper() != flight["callsign"]:
            continue
        if (type(observed) is not int or observed < flight["firstSeen"] or not 0 <= fetched - observed <= 60):
            continue
        # Aircraft and callsigns can be reused on later departures.
        arrived = flight.get("lastSeen")
        if type(arrived) is int and observed > arrived + 15 * 60:
            continue
        if not all(type(value) in (int, float) for value in (state[5], state[6])):
            continue
        position = coordinates(state[6], state[5])
        if position:
            return {**position, "source": OPEN_SKY, "timestamp": utc_iso(observed), "simulated": False}
    return None


def match_flights(shipment: dict, now: dt.datetime, line: tuple[Point, ...], *, background: bool = False) -> dict:
    origin, destination = (AIRPORT_ICAO.get(str(shipment[key]).strip().upper()) for key in ("origin", "destination"))
    try:
        departure = rules.ts(shipment["etd"])
    except ValueError:
        departure = None
    result = {
        "status": "no_match", "origin_icao": origin, "destination_icao": destination,
        "source": OPEN_SKY, "source_status": "no_data", "simulated": False,
        "candidate_count": 0, "candidates": [], "needs_flight_number": False,
    }
    if not (origin and destination and departure):
        return result

    etd = int(departure.timestamp())
    now = now if now.tzinfo else now.replace(tzinfo=rules.timezone())
    mock = os.getenv("TRACKING_MOCK") == "1"
    if mock:
        source_status, rows = "mock", []
    else:
        key = OPEN_SKY, "departure", origin, etd
        query = {"airport": origin, "begin": etd - DEPARTURE_WINDOW, "end": etd + DEPARTURE_WINDOW}
        response = cached_public(key, lambda: opensky_request("flights/departure", query), background=background)
        source_status, rows = response["status"], response.get("data", [])
    result["source_status"] = source_status
    if source_status == "pending":
        return {**result, "status": "pending"}

    simulated = mock or source_status in ("no_data", "unavailable")
    if simulated:
        rows = mock_departures(origin, destination, etd, now.timestamp(), line)
        result.update(source=SIMULATED_FLIGHTS, simulated=True)
    carrier = shipment.get("carrier") or shipment.get("carrier_id") or ""
    prefixes = CARGO_CARRIERS.get(carrier.strip().lower(), ())
    flights = ranked_departures(rows, origin, destination, etd, prefixes)
    if not flights:
        return result

    result.update(status="matched" if len(flights) == 1 else "ambiguous",
                  candidate_count=len(flights), needs_flight_number=len(flights) > 1)
    top = flights[:MAX_FLIGHT_CANDIDATES]
    position_status = "available" if simulated else "no_data"
    states = None
    if not simulated:
        icao24 = tuple(sorted({flight["icao24"] for flight in top}))
        key = OPEN_SKY, "states", icao24
        response = cached_public(key, lambda: opensky_request("states/all", {"icao24": icao24}), background=background)
        position_status, states = response["status"], response.get("data")

    for flight in top:
        position = flight.get("position") if simulated else flight_position(flight, states) if states else None
        callsign = flight["callsign"] or flight["icao24"]
        if position:
            position = {**position, "callsign": callsign}
        result["candidates"].append({
            "icao24": flight["icao24"], "callsign": callsign, "departed_at": utc_iso(flight["firstSeen"]),
            "departure_offset_minutes": (flight["firstSeen"] - etd) / 60, "position": position,
        })
    result["position_status"] = position_status
    return result


def _read_ais(key: str, mmsi: str, imo: str, center: Point) -> dict:
    try:
        from websockets.exceptions import WebSocketException
        from websockets.sync.client import connect
    except ImportError:
        return public_context(AIS_STREAM, "unavailable")
    lon, lat = center
    # Coverage is local to the scheduled corridor. IMO matching first learns
    # the transmitting MMSI from a static vessel message in the same area.
    reports = ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"]
    subscription = {
        "APIKey": key,
        "BoundingBoxes": [[[max(-90, lat - 3), max(-180, lon - 5)],
                           [min(90, lat + 3), min(180, lon + 5)]]],
        "FilterMessageTypes": reports if mmsi else [*reports, "ShipStaticData"],
    }
    if mmsi:
        subscription["FiltersShipMMSI"] = [mmsi]
    try:
        with connect("wss://stream.aisstream.io/v0/stream", open_timeout=1, close_timeout=0.2) as socket:
            socket.send(json.dumps(subscription))
            deadline = time.monotonic() + 1.5
            while (remaining := deadline - time.monotonic()) > 0:
                message = json.loads(socket.recv(timeout=remaining))
                if message.get("error"):
                    return public_context(AIS_STREAM, "unavailable")
                kind = message.get("MessageType")
                meta = message.get("MetaData") or {}
                ship_id = str(meta.get("MMSI", ""))
                body = message.get("Message", {}).get(kind, {})
                if not mmsi and kind == "ShipStaticData" and body.get("Valid", True) and str(body.get("ImoNumber")) == imo:
                    mmsi = ship_id
                if kind in reports and mmsi and ship_id == mmsi and body.get("Valid", True):
                    position = coordinates(body.get("Latitude", meta.get("Latitude")),
                                           body.get("Longitude", meta.get("Longitude")))
                    if position:
                        timestamp = utc_iso(time.time())
                        item = {**position, "kind": "vessel", "id": ship_id, "matched": True,
                                "name": (meta.get("ShipName") or ship_id).strip(), "source": AIS_STREAM,
                                "timestamp": timestamp, "timestamp_kind": "received"}
                        return public_context(AIS_STREAM, "live", [item], timestamp)
    except TimeoutError:
        return public_context(AIS_STREAM, "no_position")
    except (OSError, WebSocketException, ValueError, TypeError, AttributeError, KeyError):
        return public_context(AIS_STREAM, "unavailable")
    return public_context(AIS_STREAM, "no_position")


def ais_context(mmsi: str | None, imo: str | None, center: Point | None, *, background: bool = False) -> dict:
    key = os.getenv("AIS_API_KEY")
    if not key:
        return public_context(AIS_STREAM, "not_connected")
    mmsi, imo = str(mmsi or "").strip(), str(imo or "").upper().removeprefix("IMO").strip()
    mmsi = mmsi if len(mmsi) == 9 and mmsi.isascii() and mmsi.isdecimal() else ""
    imo = imo if len(imo) == 7 and imo.isascii() and imo.isdecimal() else ""
    if not (mmsi or imo):
        return public_context(AIS_STREAM, "missing_identity")
    if not center:
        return public_context(AIS_STREAM, "no_position")
    return cached_public((AIS_STREAM, mmsi, imo), lambda: _read_ais(key, mmsi, imo, center), background=background)


def shipment_tracking(shipment: dict, places: dict, now: dt.datetime, include_public: bool, *, background: bool = False) -> dict:
    mode = shipment["mode"]
    via = shipment["via"] or shipment["route_via"]
    origin, destination = shipment["origin"], shipment["destination"]
    stops = [places[code] for code in dict.fromkeys((origin, via, destination)) if code in places]
    line, route_source = route_geometry(mode, stops) if origin in places and destination in places else ((), "")
    reported = reported_position(shipment, stops)
    progress = schedule_progress(now, shipment["etd"], shipment["eta"])
    expected = None
    if line and progress is not None:
        (lon, lat), segment = position_along(line, progress)
        expected = {"lat": lat, "lon": lon, "segment": segment, "progress_pct": round(progress * 100, 1),
                    "source": "Schedule estimate", "timestamp": now.isoformat()}
    divergence = None
    verdict = None
    if reported and expected:
        divergence = round(distance_km(point_of(reported), point_of(expected)), 1)
    if reported and reported["plausibility"] == "implausible":
        verdict = "carrier position implausible"
    elif divergence is not None and reported["plausibility"] == "plausible":
        verdict = "feeds agree" if divergence <= AGREEMENT_KM else f"differs by {round(divergence)} km"
    # The air ETA refers to Frankfurt. The onward leg has no shipment-specific
    # timing in this feed, so it is shown separately and excluded from progress.
    site = places.get(shipment["dest_site"])
    transfer = [point_of(places[destination]), point_of(site)] if mode == "air" and site and line else []
    context = None
    held = shipment.get("held_reason")
    waiting = shipment.get("status") == "booked"
    if held or waiting or shipment.get("status") == "arrived_port":
        # Nothing is moving: don't estimate progress or match flights/vessels for a box that sits still.
        at = stops[0] if held or waiting else (places.get(destination) or (stops[-1] if stops else None))
        label = (f"held at the supplier in {at['name']}" if held else f"not picked up yet in {at['name']}" if waiting
                 else f"waiting at {at['name']}") if at else None
        current = {"lat": at["lat"], "lon": at["lon"], "source": "Supplier notice" if held else "Carrier feed",
                   "timestamp": shipment.get("updated_at"), "label": label} if at else None
        return {
            "route": line, "route_source": route_source, "stops": stops, "transfer": [], "transfer_destination": None,
            "reported": reported, "expected": None, "divergence_km": None,
            "verdict": f"held at the supplier: {held}" if held else "booked, pickup not confirmed" if waiting else "at the port, no truck booked",
            "public_context": None, "eta": shipment["eta"], "current_position": current, "flight_match": None,
            "needs_flight_number": False,
        }
    flight_match = match_flights(shipment, now, line, background=background) if include_public and mode == "air" else None
    current = expected
    if flight_match and flight_match["status"] == "matched":
        current = flight_match["candidates"][0]["position"] or expected
    if mode != "air" and reported and reported["plausibility"] == "plausible":
        current = reported
    if include_public and mode == "sea":
        center = expected or places.get(destination)
        context = ais_context(shipment.get("vessel_mmsi"), shipment.get("vessel_imo"),
                              point_of(center) if center else None, background=background)
        if context["items"]:
            current = context["items"][0]
        elif context["status"] in ("unavailable", "no_position", "pending"):
            current = expected or current
    return {
        "route": line, "route_source": route_source, "stops": stops, "transfer": transfer,
        "transfer_destination": site if transfer else None, "reported": reported, "expected": expected,
        "divergence_km": divergence, "verdict": verdict, "public_context": context,
        "eta": shipment["eta"], "current_position": current, "flight_match": flight_match,
        "needs_flight_number": bool(flight_match and flight_match["needs_flight_number"]),
    }


def snapshot(db_path: Path = rules.DB_PATH, *, include_public: bool = True) -> dict:
    now = rules.now()
    with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        places = {
            name: {"name": label, "lat": lat, "lon": lon}
            for name, label, lat, lon in conn.execute(
                "SELECT locode, name, lat, lon FROM ports UNION ALL SELECT site_id, name, lat, lon FROM sites"
            ) if coordinates(lat, lon)
        }
        places.setdefault("SGSIN", SINGAPORE)
        shipments = [dict(row) for row in conn.execute(
            "SELECT s.*, r.via AS route_via, c.name AS carrier FROM shipments s "
            "LEFT JOIN routes r USING (route_id) LEFT JOIN carriers c ON c.carrier_id = s.carrier_id"
        )]
        components = dict(conn.execute("SELECT component_id, description FROM components"))
        holds = {}
        try:  # supplier holds read from documents (e.g. "packed and on hold at our dock")
            for payload, in conn.execute("SELECT payload FROM doc_facts WHERE fact_type='supplier_hold'"):
                fact = json.loads(payload)
                if fact.get("po_number"):
                    holds[str(fact["po_number"])] = fact.get("reason") or "supplier hold"
        except sqlite3.Error:
            pass
    details = {}
    for shipment in shipments:
        shipment["held_reason"] = next((reason for po, reason in holds.items() if po in (shipment["po_numbers"] or "")), None)
        materials = (shipment["material_ids"] or "").split("|")
        cargo = "Mixed" if len(materials) > 1 else rules.plain(components.get(materials[0]))
        cargo = " ".join(word if index == 0 or word.isupper() else word.lower()
                         for index, word in enumerate(cargo.split()))
        item = shipment_tracking(shipment, places, now, include_public, background=True)
        details[shipment["shipment_id"]] = {**item, "cargo": cargo}
    return {"as_of": now.isoformat(), "shipments": details}
