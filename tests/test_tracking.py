import datetime as dt
import io
import json
from threading import Event
from urllib.parse import parse_qs, urlparse

import pytest

from core import tracking


BUSAN = {"name": "Busan", "lon": 129.0403, "lat": 35.1028}
HAMBURG = {"name": "Hamburg", "lon": 9.98, "lat": 53.54}


def test_sea_route_respects_singapore_and_runs_through_open_water():
    stops = [BUSAN, tracking.SINGAPORE, HAMBURG]
    line, _ = tracking.route_geometry("sea", stops)
    assert line[0] == tracking.point_of(BUSAN)
    assert tracking.point_of(tracking.SINGAPORE) in line
    assert line[-1] == tracking.point_of(HAMBURG)
    assert any(32 <= lon <= 33 and 30 <= lat <= 31 for lon, lat in line)  # Suez
    for progress in (0.35, 0.5, 0.9):
        point, _ = tracking.position_along(line, progress)
        assert not tracking.is_on_land(point)


def test_schedule_progress_uses_elapsed_time_and_clamps_at_endpoints():
    etd = "2026-11-20T12:00:00+01:00"
    eta = "2026-11-24T12:00:00+01:00"
    schedule = lambda now: tracking.schedule_progress(dt.datetime.fromisoformat(now), etd, eta)
    assert schedule("2026-11-21T11:00:00+00:00") == 0.25
    assert schedule("2026-11-20T11:59:00+01:00") == 0
    assert schedule("2026-11-25T12:00:00+01:00") == 1
    assert tracking.schedule_progress(dt.datetime.now(dt.UTC), eta, etd) is None
    assert tracking.schedule_progress(dt.datetime.now(dt.UTC), None, eta) is None


def test_progress_moves_by_distance_over_unequal_segments():
    point, segment = tracking.position_along(((0, 0), (1, 0), (4, 0)), 0.5)
    assert point == pytest.approx((2, 0))
    assert segment == 1


def test_inland_carrier_report_remains_visible_for_comparison():
    shipment = {
        "mode": "sea", "origin": "KRPUS", "destination": "DEHAM", "dest_site": "DC-HAM",
        "via": "SGSIN", "route_via": "", "etd": "2026-10-17", "eta": "2026-11-24",
        "status": "in_transit", "last_lat": 51.5992, "last_lon": 22.5127,
        "updated_at": "2026-11-20T15:00:00+01:00",
    }
    places = {"KRPUS": BUSAN, "DEHAM": HAMBURG, "SGSIN": tracking.SINGAPORE}
    now = dt.datetime.fromisoformat("2026-11-21T19:00:00+01:00")
    result = tracking.shipment_tracking(shipment, places, now, include_public=False)
    assert result["reported"]["lat"] == shipment["last_lat"]
    assert result["reported"]["plausibility"] == "implausible"
    assert result["verdict"] == "carrier position implausible"
    assert result["divergence_km"] > 1000
    assert not tracking.is_on_land(tracking.point_of(result["expected"]))

    shipment.update(status="arrived_port", last_lat=HAMBURG["lat"], last_lon=HAMBURG["lon"])
    assert tracking.reported_position(shipment, [BUSAN, HAMBURG])["plausibility"] == "plausible"


def test_air_arc_bows_north_and_road_transfer_starts_at_the_timed_destination():
    chicago = {"name": "Chicago", "lat": 41.9742, "lon": -87.9073}
    frankfurt = {"name": "Frankfurt", "lat": 50.0379, "lon": 8.5622}
    shipment = {
        "mode": "air", "origin": "ORD", "destination": "FRA", "dest_site": "DC-HAM",
        "via": "", "route_via": "", "etd": "2026-11-19", "eta": "2026-11-21",
        "last_lat": None, "last_lon": None, "status": "in_transit", "updated_at": None,
    }
    places = {"ORD": chicago, "FRA": frankfurt, "DC-HAM": HAMBURG}
    now = dt.datetime.fromisoformat("2026-11-21T12:00:00+01:00")
    data = tracking.shipment_tracking(shipment, places, now, include_public=False)
    assert max(lat for _, lat in data["route"]) > 50
    assert tracking.point_of(data["expected"]) == data["route"][-1] == data["transfer"][0]
    assert data["transfer"][1] == tracking.point_of(HAMBURG)


@pytest.fixture
def air_shipment():
    shipment = {
        "mode": "air", "origin": "ORD", "destination": "FRA", "dest_site": "DC-HAM",
        "via": "", "route_via": "", "etd": "2026-11-19T11:00:00+00:00", "eta": "2026-11-20",
        "carrier_id": "LSP-02", "last_lat": 41.97, "last_lon": -87.90,
        "status": "in_transit", "updated_at": "2026-11-19T11:00:00+00:00",
    }
    places = {
        "ORD": {"name": "Chicago", "lat": 41.9742, "lon": -87.9073},
        "ICN": {"name": "Incheon", "lat": 37.4602, "lon": 126.4407},
        "FRA": {"name": "Frankfurt", "lat": 50.0379, "lon": 8.5622},
        "DC-HAM": HAMBURG,
    }
    now = dt.datetime.fromisoformat("2026-11-19T18:00:00+00:00")
    return shipment, places, now


@pytest.fixture
def opensky(monkeypatch, air_shipment):
    shipment, _, now = air_shipment
    etd = int(dt.datetime.fromisoformat(shipment["etd"]).timestamp())
    stamp = int(now.timestamp())
    rows, states, calls = [], [], []

    def read(url, *, timeout):
        assert timeout == 3
        request = urlparse(url)
        calls.append((request.path, parse_qs(request.query)))
        payload = rows if request.path.endswith("/departure") else {"time": stamp, "states": states}
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.delenv("TRACKING_MOCK", raising=False)
    monkeypatch.delenv("OPENSKY_CLIENT_ID", raising=False)
    monkeypatch.setattr(tracking, "public_cache", {})
    monkeypatch.setattr(tracking.time, "time", lambda: stamp)
    monkeypatch.setattr(tracking, "urlopen", read)
    return etd, stamp, rows, states, calls


def departure(icao24, callsign, timestamp, origin="KORD", destination="EDDF"):
    return {"icao24": icao24, "callsign": callsign, "firstSeen": timestamp,
            "estDepartureAirport": origin, "estArrivalAirport": destination}


def test_unique_flight_uses_matching_live_position_and_caches_both_requests(air_shipment, opensky):
    etd, stamp, departures, states, calls = opensky
    flight = departure("a12345", " GEC8221 ", etd + 60)
    departures.extend([flight, flight.copy()])
    states.extend([
        ["a12345", " GEC8229 ", "", stamp - 1, stamp, 20.0, 55.0, None, False],
        ["a12345", " GEC8221 ", "", stamp - 4, stamp, 8.6, 50.02, None, False],
    ])

    result = tracking.shipment_tracking(*air_shipment, include_public=True)
    assert result["flight_match"]["status"] == "matched"
    assert result["flight_match"]["candidate_count"] == 1
    assert not result["needs_flight_number"]
    assert result["current_position"] == {
        "lat": 50.02, "lon": 8.6, "callsign": "GEC8221", "source": tracking.OPEN_SKY,
        "timestamp": tracking.utc_iso(stamp - 4), "simulated": False,
    }
    assert tracking.shipment_tracking(*air_shipment, include_public=True) == result
    assert calls == [
        ("/api/flights/departure", {"airport": ["KORD"], "begin": [str(etd - 43200)], "end": [str(etd + 43200)]}),
        ("/api/states/all", {"icao24": ["a12345"]}),
    ]


def test_ambiguous_flights_are_ranked_and_need_flight_number(air_shipment, opensky):
    etd, stamp, departures, states, calls = opensky
    departures.extend([
        departure("a12345", "GEC8221", etd - 600),
        departure("a12346", "GTI8314", etd + 120),
    ])
    states.extend([
        ["a12345", "GEC8221", "", stamp - 4, stamp, -30, 52.0, None, False],
        ["a12346", "GTI8314", "", stamp - 8, stamp, -60, 52.0, None, False],
    ])

    result = tracking.shipment_tracking(*air_shipment, include_public=True)
    flight = result["flight_match"]
    assert flight["status"] == "ambiguous"
    assert flight["candidate_count"] == 2
    assert flight["needs_flight_number"] and result["needs_flight_number"]
    assert [c["callsign"] for c in flight["candidates"]] == ["GTI8314", "GEC8221"]
    assert [c["departure_offset_minutes"] for c in flight["candidates"]] == [2, -10]
    assert all(c["position"] for c in flight["candidates"])
    assert result["current_position"] == result["expected"]
    assert calls[-1] == ("/api/states/all", {"icao24": ["a12345", "a12346"]})


def test_wrong_airports_and_departure_times_fall_back_to_schedule(air_shipment, opensky):
    etd, _, departures, _, calls = opensky
    departures.extend([
        departure("a12345", "GEC8221", etd, destination="EDDM"),
        departure("a12346", "GTI8314", etd, origin="KJFK"),
        departure("a12347", "GEC8223", etd, destination=None),
        departure("a12348", "GEC8225", etd + 43201),
    ])

    result = tracking.shipment_tracking(*air_shipment, include_public=True)
    assert result["flight_match"]["status"] == "no_match"
    assert not result["flight_match"]["candidates"]
    assert not result["needs_flight_number"]
    assert result["current_position"] == result["expected"]
    assert len(calls) == 1


def test_known_operator_filters_callsigns_and_old_flights_cannot_reuse_positions(air_shipment, opensky):
    shipment, _, _ = air_shipment
    shipment["carrier"] = "Lufthansa Cargo"
    etd, stamp, departures, states, _ = opensky
    departures.extend([
        departure("a12345", "GTI8314", etd),
        {**departure("a12346", "GEC8221", etd), "lastSeen": stamp - 3600},
    ])
    states.append(["a12346", "GEC8221", "", stamp - 1, stamp, 8.6, 50.02, None, False])

    result = tracking.shipment_tracking(*air_shipment, include_public=True)
    assert result["flight_match"]["status"] == "matched"
    assert result["flight_match"]["candidates"][0]["callsign"] == "GEC8221"
    assert result["flight_match"]["candidates"][0]["position"] is None
    assert result["current_position"] == result["expected"]


@pytest.mark.parametrize("source_status", ["mock", "no_data", "unavailable"])
def test_simulated_air_source_covers_both_outcomes(monkeypatch, air_shipment, opensky, source_status):
    shipment, places, now = air_shipment
    if source_status == "mock":
        monkeypatch.setenv("TRACKING_MOCK", "1")
    if source_status == "unavailable":
        def offline(*_, **__):
            raise TimeoutError("offline")

        monkeypatch.setattr(tracking, "urlopen", offline)
    now += dt.timedelta(days=1)

    unique = tracking.shipment_tracking(shipment, places, now, include_public=True)
    ambiguous = tracking.shipment_tracking({**shipment, "origin": "ICN"}, places, now, include_public=True)
    assert unique["flight_match"]["status"] == "matched"
    assert unique["flight_match"]["source_status"] == source_status
    assert unique["current_position"]["simulated"]
    assert ambiguous["flight_match"]["status"] == "ambiguous"
    assert ambiguous["flight_match"]["simulated"]
    assert ambiguous["needs_flight_number"]
    assert [c["callsign"] for c in ambiguous["flight_match"]["candidates"]] == ["KAL529", "GEC8471"]
    assert ambiguous["current_position"] == ambiguous["expected"]
    if source_status == "mock":
        assert not opensky[-1]


def test_background_tracking_returns_schedule_while_provider_is_pending(monkeypatch, air_shipment, opensky):
    release = Event()
    read = tracking.urlopen

    def delayed(*args, **kwargs):
        assert release.wait(3)
        return read(*args, **kwargs)

    monkeypatch.setattr(tracking, "urlopen", delayed)
    try:
        result = tracking.shipment_tracking(*air_shipment, include_public=True, background=True)
        assert result["flight_match"]["status"] == "pending"
        assert result["current_position"] == result["expected"]
    finally:
        release.set()

    assert tracking.shipment_tracking(*air_shipment, include_public=True)["flight_match"]["simulated"]
    assert len(opensky[-1]) == 1


@pytest.mark.parametrize("mmsi, imo", [("123456789", None), (None, "IMO 1234567")])
def test_ais_matches_only_the_requested_vessel(monkeypatch, mmsi, imo):
    client = pytest.importorskip("websockets.sync.client")
    subscriptions = []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def send(self, payload):
            subscriptions.append(json.loads(payload))

        def recv(self, *, timeout):
            return json.dumps(next(messages))

    messages = iter([
        {"MessageType": "ShipStaticData", "MetaData": {"MMSI": 123456789},
         "Message": {"ShipStaticData": {"ImoNumber": 1234567}}},
        {"MessageType": "PositionReport", "MetaData": {"MMSI": 987654321},
         "Message": {"PositionReport": {"Latitude": 53.55, "Longitude": 9.91}}},
        {"MessageType": "PositionReport", "MetaData": {"MMSI": 123456789, "ShipName": "Example vessel"},
         "Message": {"PositionReport": {"Latitude": 53.54, "Longitude": 9.90}}},
    ])
    monkeypatch.setenv("AIS_API_KEY", "test-key")
    monkeypatch.setattr(tracking, "public_cache", {})
    monkeypatch.setattr(client, "connect", lambda *_args, **_kwargs: Socket())
    result = tracking.ais_context(mmsi, imo, tracking.point_of(HAMBURG))
    assert result["status"] == "live"
    assert result["items"][0]["id"] == "123456789"
    assert result["items"][0]["matched"]
    assert "APIKey" not in json.dumps(result)
    if mmsi:
        assert subscriptions[0]["FiltersShipMMSI"] == [mmsi]
    else:
        assert "ShipStaticData" in subscriptions[0]["FilterMessageTypes"]


def test_held_shipment_is_not_matched_to_a_flight():
    import datetime as dt
    from core import tracking
    places = {"ORD": {"name": "Chicago O'Hare", "lat": 41.97, "lon": -87.9}, "FRA": {"name": "Frankfurt", "lat": 50.03, "lon": 8.56},
              "DC-HAM": {"name": "DC Hamburg", "lat": 53.55, "lon": 9.99}}
    shipment = {"shipment_id": "S", "mode": "air", "via": None, "route_via": None, "origin": "ORD", "destination": "FRA", "dest_site": "DC-HAM",
                "etd": "2026-11-21", "eta": "2026-11-23", "status": "booked", "last_lat": 41.97, "last_lon": -87.9,
                "updated_at": "2026-11-20T16:00:00+01:00", "held_reason": "terms not agreed", "carrier_id": "LSP-02", "reference": "020-1"}
    out = tracking.shipment_tracking(shipment, places, dt.datetime(2026, 11, 21, 21, tzinfo=dt.timezone.utc), True)
    assert out["flight_match"] is None and not out["needs_flight_number"]
    assert out["current_position"]["label"].startswith("Held at the supplier")
    assert out["verdict"].startswith("held at the supplier")
