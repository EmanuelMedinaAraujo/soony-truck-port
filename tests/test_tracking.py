import datetime as dt
import io
import json

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


def test_opensky_caches_timestamped_aircraft_and_degrades_offline(monkeypatch):
    stamp = 1_795_291_530
    calls = []

    def open_snapshot(url, *, timeout):
        calls.append(url)
        return io.BytesIO(json.dumps({"time": stamp, "states": [
            ["airborne", " Example ", "", stamp - 4, stamp, 8.6, 50.02, None, False],
            ["ground", "", "", stamp, stamp, 8.6, 50.02, None, True],
        ]}).encode())

    monkeypatch.setattr(tracking, "public_cache", {})
    monkeypatch.setattr(tracking.time, "time", lambda: stamp)
    monkeypatch.setattr(tracking, "urlopen", open_snapshot)
    live = tracking.opensky_context()
    assert live["status"] == "live"
    assert len(live["items"]) == 1
    assert live["items"][0]["timestamp"] == tracking.utc_iso(stamp - 4)
    assert live["items"][0]["matched"] is False
    assert tracking.opensky_context() == live
    assert len(calls) == 1

    def offline(*_, **__):
        raise OSError("offline")

    tracking.public_cache.clear()
    monkeypatch.setattr(tracking, "urlopen", offline)
    assert tracking.opensky_context()["status"] == "unavailable"
    monkeypatch.delenv("AIS_API_KEY", raising=False)
    assert tracking.ais_context(None, None, None)["status"] == "not_connected"


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
