import datetime as dt
import sqlite3
import unittest

import pytest

from core import actions
from core.dispatch import action_next_step, embedded_components, forecast, linked_actions, shipment_progress, shipment_tracking
from core.inbox import REPLY_SCHEMA


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
            "insert": {"usable_now": 0, "included_in": {"componentId": "wheel"}},
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


@pytest.mark.parametrize(("stock", "early", "late", "projected"), [
    (0, [0, 0], [0, 0], [0, 0]),
    (25, [11, 0], [21, 0], [25, 0]),
    (200, [11, 22], [21, 42], [50, 100]),
])
def test_dispatch_replays_fixed_stock_at_two_clocks(tmp_path, stock, early, late, projected):
    from core import dispatch, ingest, rules

    path = tmp_path / "master.db"
    with sqlite3.connect(path) as db:
        db.executescript(ingest.SCHEMA)
        db.executescript("""
            INSERT INTO launch_status VALUES
                ('standard', '2026-11-22T07:00:00+01:00', 100, 0, 100, 120, 'packing'),
                ('collector', '2026-11-22T07:00:00+01:00', 50, 0, 50, 60, 'packing');
            INSERT INTO bom(parent_id, child_id, qty_per) VALUES
                ('standard', 'cards', 1), ('collector', 'cards', 1), ('collector', 'book', 1);
            INSERT INTO stock(material_id, quantity) VALUES ('book', 100);
        """)
        db.execute("INSERT INTO stock(material_id, quantity) VALUES ('cards', ?)", (stock,))
    policy = rules.CONFIG | {
        "priority_product": {"value": "collector"},
        "deadline": {"value": "2026-11-22T09:00:00+01:00"},
        "shifts": {"value": [{"start": "2026-11-22T07:00:00+01:00", "end": "2026-11-22T09:00:00+01:00"}]},
        "missed_copy_cost_eur": {"value": {"collector": 120, "standard": 0}},
    }
    for clock, packed in (("07:11", early), ("07:21", late)):
        with rules.replay(policy, rules.ts(f"2026-11-22T{clock}:00+01:00")):
            snap = dispatch.snapshot(None, db_path=path, include_activity=False)
        assert [row["packed"] for row in snap["editions"]] == packed
        assert [row["projected"] for row in snap["editions"]] == projected
        cards = next(row for row in snap["components"] if row["id"] == "cards")
        assert cards["usable"] == stock - sum(packed)

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


class NextStepTest(unittest.TestCase):
    def setUp(self):
        self.log = sqlite3.connect(":memory:")
        self.addCleanup(self.log.close)
        self.log.row_factory = sqlite3.Row
        self.log.executescript(actions.SCHEMA + REPLY_SCHEMA)
        self.log.executescript("""
            INSERT INTO actions (id, finding_id, title, kind, status, updated_at) VALUES
                ('initial', 'offer-supplier-book', 'Reserve the stock', 'reserve', 'auto', '2026-11-21T18:00:00+01:00'),
                ('followup', 'reply-original', 'Confirm vans', 'ask', 'auto', '2026-11-21T18:15:00+01:00');
            INSERT INTO messages (id, action_id, channel, to_name, status, sent_at) VALUES
                ('reservation', 'initial', 'email', 'Supplier', 'sent', '2026-11-21T18:01:00+01:00'),
                ('vans', 'followup', 'email', 'Supplier', 'sent', '2026-11-21T18:16:00+01:00');
            INSERT INTO replies (id, msg_id, action_id, received_at, handled) VALUES
                ('original', 'reservation', 'initial', '2026-11-21T18:10:00+01:00', 1);
        """)

    def current(self):
        return action_next_step(linked_actions(self.log)["offer-supplier-book"], "Supplier")

    def test_follow_up_tracks_the_message_and_stops_waiting_when_reply_arrives(self):
        self.assertEqual(self.current(), {
            "state": "Waiting for reply", "text": "Sent the van request to Supplier at 18:16.",
            "since": "2026-11-21T18:16:00+01:00", "action_id": "followup",
        })
        self.log.execute("""INSERT INTO replies (id, msg_id, action_id, received_at, handled)
                          VALUES ('answer', 'vans', 'followup', '2026-11-21T17:22:00+00:00', 0)""")
        self.assertEqual(self.current()["text"], "Dockside is reading the reply from Supplier.")
        self.log.execute("UPDATE replies SET handled=1 WHERE id='answer'")
        self.assertIsNone(self.current())

    def test_distinguishes_queued_mail_unknown_delivery_and_approval(self):
        self.log.execute("UPDATE messages SET status='outbox', sent_at=NULL WHERE id='vans'")
        self.assertEqual(self.current()["state"], "Asking")
        self.log.execute("UPDATE messages SET status='delivery_unknown' WHERE id='vans'")
        self.assertEqual(self.current()["state"], "Stuck")
        self.log.execute("UPDATE actions SET status='proposed', kind='dock_slot' WHERE id='followup'")
        self.assertEqual(self.current()["state"], "Needs you")
        self.assertEqual(self.current()["text"], "Approve the unload slot.")

    def test_tracker_moves_independently_of_a_pending_request(self):
        now = dt.datetime.fromisoformat("2026-11-21T18:00:00+01:00")
        shipment = {"status": "booked", "updated_at": "2026-11-21T17:00:00+01:00", "flags": [],
                    "arrival_at_dc": "2026-11-22T12:00:00+01:00", "makes_cutoff": True}
        for status, hold, stage, expected_state in (
            ("booked", "supplier hold", "confirm", "Stuck"),
            ("booked", None, "ship", "Booked"),
            ("arrived_port", None, "track", "Stuck"),
            ("in_transit", None, "track", "On the way"),
            ("arrived_dc", None, "receive", "Confirmed"),
            ("delivered", None, "receive", "Unloaded"),
        ):
            with self.subTest(status=status, hold=hold):
                current, action = shipment_progress(shipment | {"status": status}, [], "Supplier", hold, now)
                self.assertEqual(current, stage)
                self.assertEqual(action["state"], expected_state)


def test_snapshot_reports_the_configured_dispatch_deadline():
    import pytest
    from core import dispatch, rules
    if not rules.DB_PATH.exists():
        pytest.skip("needs the local Soony snapshot")
    snap = dispatch.snapshot(None)
    deadline = rules.ts(rules.cfg("deadline"))
    assert rules.ts(snap["deadline"]) == deadline
    assert rules.ts(snap["cutoff"]) < deadline


def test_supplier_acceptance_is_recognised_but_refusal_is_not():
    from core.release import _claim_rows

    yes = "Karton Nowak says it is treating the prior written message as the EUR 16,800 order and has released the goods to logistics"
    no = "Karton Nowak cannot accept the order until a PO number is given"
    reply = {"id": "RPL-1", "from_name": "Karton Nowak", "received_at": "2026-11-22T02:07:00+01:00",
             "summary_en": None}
    assert _claim_rows(dict(reply, body=yes), {})[0]["kind"] == "accepted"
    assert _claim_rows(dict(reply, body=no), {})[0]["kind"] == "disputed"
