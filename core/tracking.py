"""Carrier reports, schedule estimates, and public traffic around a shipment route."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3
import time
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from core import rules
from core.dispatch import coordinates

Point = tuple[float, float]  # longitude, latitude (GeoJSON order)
SINGAPORE = {"name": "Singapore", "lat": 1.1714, "lon": 103.8611}
AGREEMENT_KM = 25
PUBLIC_CACHE_SECONDS = 240  # One OpenSky credit per snapshot, up to 360 per day.
OPEN_SKY = "OpenSky Network"
AIS_STREAM = "AIS Stream"
FRANKFURT = (8.5622, 50.0379)


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
    now = now if now.tzinfo else now.replace(tzinfo=rules.TZ)
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
    # Hamburg terminal and truck pickup also share the port's ERP coordinates.
    sea = shipment["mode"] == "sea"
    inland = sea and is_on_land(point) and all(distance_km(point, point_of(stop)) > 20 for stop in stops)
    plausibility = "implausible" if inland else "unconfirmed" if shipment["status"] == "booked" else "plausible"
    return {**position, "source": "Carrier feed", "timestamp": shipment["updated_at"],
            "plausibility": plausibility, "simulated": sea}


public_lock = Lock()
public_cache: dict[tuple, tuple[float, dict]] = {}


def public_context(source: str, status: str, items: list | None = None, timestamp: str | None = None) -> dict:
    return {"source": source, "status": status, "timestamp": timestamp, "items": items or []}


def utc_iso(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.UTC).isoformat(timespec="seconds")


def opensky_context() -> dict:
    with public_lock:
        key = (OPEN_SKY,)
        expires, cached = public_cache.get(key, (0, None))
        if time.monotonic() < expires:
            return cached
        query = urlencode({"lamin": 48.8, "lomin": 6.5, "lamax": 51.3, "lomax": 10.6})
        retry = PUBLIC_CACHE_SECONDS
        try:
            with urlopen(f"https://opensky-network.org/api/states/all?{query}", timeout=3) as response:
                data = json.load(response)
            fetched_at = data["time"]
            aircraft = []
            for state in data.get("states") or []:
                if len(state) < 9 or state[3] is None or state[8] or fetched_at - state[3] > 60:
                    continue
                position = coordinates(state[6], state[5])
                if position:
                    aircraft.append({
                        **position, "kind": "aircraft", "id": state[0], "name": (state[1] or state[0]).strip(),
                        "source": OPEN_SKY, "timestamp": utc_iso(state[3]), "matched": False,
                    })
            aircraft.sort(key=lambda item: distance_km(point_of(item), FRANKFURT))
            status = "stale" if time.time() - fetched_at > PUBLIC_CACHE_SECONDS else "live"
            result = public_context(OPEN_SKY, status, aircraft[:24], utc_iso(fetched_at))
        except HTTPError as error:
            delay = (error.headers or {}).get("X-Rate-Limit-Retry-After-Seconds", "")
            retry = max(retry, int(delay)) if delay.isdecimal() else retry
            error.close()
            result = public_context(OPEN_SKY, "unavailable")
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            result = public_context(OPEN_SKY, "unavailable")
        public_cache[key] = time.monotonic() + retry, result
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
        with connect("wss://stream.aisstream.io/v0/stream", open_timeout=2, close_timeout=0.2) as socket:
            socket.send(json.dumps(subscription))
            deadline = time.monotonic() + 2
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
    except (OSError, WebSocketException, ValueError, TypeError):
        return public_context(AIS_STREAM, "unavailable")
    return public_context(AIS_STREAM, "no_position")


def ais_context(mmsi: str | None, imo: str | None, center: Point | None) -> dict:
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
    with public_lock:
        cache_key = AIS_STREAM, mmsi, imo
        expires, cached = public_cache.get(cache_key, (0, None))
        if time.monotonic() < expires:
            return cached
        result = _read_ais(key, mmsi, imo, center)
        public_cache[cache_key] = time.monotonic() + PUBLIC_CACHE_SECONDS, result
        return result


def shipment_tracking(shipment: dict, places: dict, now: dt.datetime, include_public: bool) -> dict:
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
    if include_public and mode == "air" and destination == "FRA":
        context = opensky_context()
    if include_public and mode == "sea":
        center = expected or places.get(destination)
        context = ais_context(shipment.get("vessel_mmsi"), shipment.get("vessel_imo"), point_of(center) if center else None)
    return {
        "route": line, "route_source": route_source, "stops": stops, "transfer": transfer,
        "transfer_destination": site if transfer else None, "reported": reported, "expected": expected,
        "divergence_km": divergence, "verdict": verdict, "public_context": context,
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
            "SELECT s.*, r.via AS route_via FROM shipments s LEFT JOIN routes r USING (route_id)"
        )]
    return {"as_of": now.isoformat(), "shipments": {
        row["shipment_id"]: shipment_tracking(row, places, now, include_public) for row in shipments
    }}
