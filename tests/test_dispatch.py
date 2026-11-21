import sqlite3
import unittest

from core.dispatch import embedded_components, forecast, shipment_tracking


class DispatchForecastTest(unittest.TestCase):
    def test_delivery_can_move_the_bottleneck_to_another_part(self):
        picture = {"collector": {"todo_now": 12_000, "capacity_left": 16_000, "packed_est_now": 60_000, "demand": 72_000}}
        coverage = {
            "book": {"usable_now": 6_100, "suspect_master_data": False},
            "statue": {"usable_now": 8_600, "suspect_master_data": False},
        }
        bom = {part: {"collector": 1} for part in coverage}
        before, _ = forecast(picture, coverage, bom, "collector")
        after, _ = forecast(picture, coverage, bom, "collector", {"book": 8_000})

        self.assertEqual(before["collector"]["constraintIds"], ["book"])
        self.assertEqual(after["collector"]["constraintIds"], ["statue"])
        self.assertEqual(after["collector"]["projected"] - before["collector"]["projected"], 2_500)

    def test_both_toy_kits_draw_from_one_stock_pool_at_their_bom_ratios(self):
        picture = {
            "starter-kit": {"todo_now": 100, "capacity_left": 110, "packed_est_now": 0, "demand": 100},
            "gift-kit": {"todo_now": 30, "capacity_left": 35, "packed_est_now": 0, "demand": 30},
        }
        coverage = {
            "wheel": {"usable_now": 250, "suspect_master_data": False},
            "insert": {"usable_now": 0, "suspect_master_data": True},
        }
        bom = {"wheel": {"starter-kit": 2, "gift-kit": 4}, "insert": {"gift-kit": 1}}
        forecasted, allocations = forecast(picture, coverage, bom, "gift-kit")

        self.assertEqual(allocations["wheel"]["gift-kit"]["allocated"], 120)
        self.assertEqual(allocations["wheel"]["starter-kit"]["allocated"], 130)
        self.assertEqual(forecasted["gift-kit"]["projected"], 30)
        self.assertEqual(forecasted["starter-kit"]["projected"], 65)

    def test_packing_capacity_and_existing_stock_still_cap_the_recovery(self):
        picture = {"kit": {"todo_now": 100, "capacity_left": 60, "packed_est_now": 50, "demand": 150}}
        coverage = {"part": {"usable_now": -6, "suspect_master_data": False}}
        forecasted, allocations = forecast(picture, coverage, {"part": {"kit": 1}}, "kit", {"part": 90})

        self.assertEqual(allocations["part"]["kit"]["allocated"], 90)
        self.assertEqual(forecasted["kit"]["projected"], 110)
        self.assertTrue(forecasted["kit"]["capacityLimited"])


class SupplyTraceTest(unittest.TestCase):
    def test_supplier_fitted_part_attaches_to_its_documented_parent(self):
        with sqlite3.connect(":memory:") as conn:
            conn.executescript("""CREATE TABLE suppliers(supplier_id TEXT, name TEXT, folder TEXT);
                CREATE TABLE supplier_components(supplier_id TEXT, component_id TEXT, note TEXT);
                INSERT INTO suppliers VALUES ('supplier','Packaging supplier','packaging');
                INSERT INTO supplier_components VALUES ('supplier','magnet','fitted by supplier into box; stocked with the box');""")
            coverage = {"magnet": {"suspect_master_data": True}, "box": {"suspect_master_data": False}}
            self.assertEqual(embedded_components(conn, coverage)["magnet"]["componentId"], "box")
            coverage["magnet"]["suspect_master_data"] = False
            self.assertEqual(embedded_components(conn, coverage), {})

    def test_booking_is_not_a_location_and_port_event_uses_the_known_terminal(self):
        shipment = {"shipment_id": "shipment", "reference": "container", "mode": "sea", "origin": "CNPORT",
                    "destination": "DEPORT", "status": "booked", "last_lat": 22, "last_lon": 114,
                    "updated_at": "2026-11-20T10:00:00+01:00", "arrival_at_dc": None, "flags": []}
        places = {"CNPORT": {"name": "Origin", "lat": 22, "lon": 114}, "DEPORT": {"name": "Terminal", "lat": 53, "lon": 10}}
        self.assertIsNone(shipment_tracking(shipment, places, None)["position"])
        shipment["status"] = "arrived_port"
        self.assertEqual(shipment_tracking(shipment, places, None)["position"], {"lat": 53, "lon": 10})
        self.assertIsNone(shipment_tracking(shipment, places, "Escalated supplier hold")["position"])
