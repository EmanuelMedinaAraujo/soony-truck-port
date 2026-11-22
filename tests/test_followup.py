import datetime as dt
import json
import sqlite3
from unittest import mock

import pytest

from core import actions, agent, customer, followup, ingest, release, rules
from examples import replay_gate

NOW = dt.datetime(2026, 11, 22, 0, 0, tzinfo=rules.TZ)
ON = {"enabled": True, "after_hours": 2, "max_per_cycle": 3}


def step(state, since=None):
    return {"state": state, "text": "x", "since": since, "action_id": None}


@mock.patch.object(followup, "settings", return_value=ON)
def test_due_after_quiet_hours(_):
    since = (NOW - dt.timedelta(hours=1)).isoformat()
    assert followup.next_check("P", step("Waiting for reply", since), {}, NOW) == NOW + dt.timedelta(hours=1)


@mock.patch.object(followup, "settings", return_value=ON)
def test_never_seen_is_due_now_not_sliding(_):
    assert followup.next_check("P", step("Stuck"), {}, NOW) == NOW


@mock.patch.object(followup, "settings", return_value=ON)
def test_deadline_pulls_follow_up_forward_and_stops_after(_):
    since = NOW.isoformat()
    deadline = NOW + dt.timedelta(hours=2)
    assert followup.next_check("P", step("Waiting for reply", since), {}, NOW, deadline) == NOW + dt.timedelta(hours=1)
    assert followup.next_check("P", step("Waiting for reply", since), {}, deadline, deadline) is None


@mock.patch.object(followup, "settings", return_value=ON)
def test_not_a_stall(_):
    for state in ("Booked", "Asking", "On the way", "Confirmed", "After launch"):
        assert followup.next_check("P", step(state, NOW.isoformat()), {}, NOW) is None


@mock.patch.object(followup, "settings", return_value=ON | {"enabled": False})
def test_off_means_no_promise(_):
    assert followup.promise("P", step("Stuck"), {}, NOW) == ""


@mock.patch.object(followup, "settings", return_value=ON)
def test_unconfirmed_supplier_report_gets_checked(_):
    report = step("On the way", (NOW - dt.timedelta(hours=3)).isoformat()) | {"unverified": True}
    assert followup.next_check("P", report, {}, NOW) <= NOW


@pytest.mark.parametrize(("deliver_by", "error"), [
    ("07:00", "inside arrival_window"),
    ("12:00", "inside arrival_window"),
    ("16:01", "inside arrival_window"),
    ("14:29", "at or after the verified carrier ETA"),
])
def test_agent_rejects_inconsistent_amendment_arrival(tmp_path, monkeypatch, deliver_by, error):
    replay = replay_gate
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    monkeypatch.setattr(rules, "DB_PATH", tmp_path / "master.db")
    policy = customer.load(replay.ROOT / "config" / "business.json")
    policy["release_carrier_contacts"] = {"value": {replay.ORDER: replay.CARRIER_EMAIL}}
    with rules.replay(policy, replay.at("00:14")):
        replay.seed_sent_order()
        carrier = replay.thread("Warta Trans", replay.CARRIER_EMAIL, "Please confirm transport.", "book_truck", replay.ORDER)
    with rules.replay(policy, replay.at("03:21")):
        replay.store_reply(carrier, "late-eta", "Carrier confirmed booking WT-123, ETA Sun 14:30. Gross vehicle weight is 7.5 t.")
        tools = agent.Tools()
        terms = tools.get_action(replay.ORDER)["delivery_terms"]
        result = tools.propose_action(
            f"followup-{replay.FINDING}-202611220321", "Amend the art book delivery deadline", "", "", "order",
            16800, 708000, 5900, "Print & Packaging", "Dockside", 15,
            [replay.email("Karton Nowak", replay.SUPPLIER_EMAIL, "Change the arrival time on the existing order.")],
            commitment={**terms, "deliver_by": replay.at(deliver_by).isoformat(), "revises_action_id": replay.ORDER},
            delivery_id=replay.ORDER,
        )
        assert error in result["error"]
        assert not actions.list_actions(("proposed",))


def test_departure_deadline_blocks_late_proposal_and_signature(tmp_path, monkeypatch):
    replay = replay_gate
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    monkeypatch.setattr(rules, "DB_PATH", tmp_path / "master.db")
    policy = customer.load(replay.ROOT / "config" / "business.json")
    policy["release_carrier_contacts"] = {"value": {replay.ORDER: replay.CARRIER_EMAIL}}
    policy["staff_email"]["value"]["Katrin Jansen"] = replay.DOCK_EMAIL
    with rules.replay(policy, replay.at("00:14")):
        replay.seed_sent_order()
        original = actions.get(replay.ORDER)
        terms = release.delivery_terms(original)
        carrier = replay.thread("Warta Trans", replay.CARRIER_EMAIL, "Please confirm transport.", "book_truck", replay.ORDER)
        dock = replay.thread("Katrin Jansen", replay.DOCK_EMAIL, "Please confirm unloading.", "dock_slot", replay.ORDER)

    def propose(stamp):
        return actions.propose(
            f"followup-{replay.FINDING}-{stamp}", "Amend the delivery deadline", "", "order", 16800, 0, 0,
            "Print & Packaging", "Dockside", 15, parent=original,
            payload={"commitment": {**terms, "deliver_by": replay.at("15:00").isoformat(),
                                    "revises_action_id": replay.ORDER, "depart_by": replay.at("09:00").isoformat()}},
            messages=[replay.email("Karton Nowak", replay.SUPPLIER_EMAIL, "Change the arrival time on the existing order.")],
        )

    with rules.replay(policy, replay.at("07:00")):
        in_time = propose("202611220700")
        assert release.terms_for(in_time)["depart_by"] == replay.at("07:00").isoformat()
        assert in_time["held_reason"] is None
    with rules.replay(policy, replay.at("07:01")):
        replay.store_reply(dock, "slot", "Dock slot confirmed for Sun 14:00–15:00.")
        replay.store_reply(carrier, "planned", "Carrier confirmed booking WT-123, ETA Sun 14:30. "
                           "Truck should have departed at 06:40. Gross vehicle weight is 7.5 t.")
        late = propose("202611220701")
        assert late["held_reason"] == "departure deadline passed"
        with pytest.raises(ValueError, match="departure deadline passed"):
            replay.sign(in_time)
        assert not actions.get(in_time["id"])["signatures"]

        replay.store_reply(carrier, "wrong-window", "Truck departed at 06:40. ETA Sun 15:01.")
        with pytest.raises(ValueError, match="departure deadline passed"):
            replay.sign(in_time)

        replay.store_reply(carrier, "verified", "Truck departed at 06:40. ETA Sun 14:30.")
        assert replay.sign(in_time)["status"] == "approved"
        assert release.decision_state(actions.get(late["id"]))["can_approve"]

        feed = replay.thread("Warta Trans", replay.CARRIER_EMAIL, "Please send the latest ETA.", "book_truck", replay.ORDER)
        replay.store_reply(feed, "feed-eta", "Carrier confirmed booking WT-123, ETA Sun 14:30. Gross vehicle weight is 7.5 t.")
        with sqlite3.connect(rules.DB_PATH) as db:
            db.execute("INSERT INTO carriers(carrier_id) VALUES ('WARTA')")
            db.execute("""INSERT INTO shipments(shipment_id,carrier_id,mode,reference,supplier_id,material_ids,
                                                dest_site,status,last_lat,last_lon,updated_at)
                          VALUES ('TRUCK-1','WARTA','road','WT-123',?,?,'DC-HAM','in_transit',52.4,16.8,?)""",
                       (replay.SUPPLIER, replay.PART, replay.at("07:01").isoformat()))
        with pytest.raises(ValueError, match="departure deadline passed"):
            replay.sign(late)
        with sqlite3.connect(rules.DB_PATH) as db:
            db.execute("UPDATE shipments SET updated_at=?", (replay.at("06:40").isoformat(),))
        assert replay.sign(late)["status"] == "approved"


def test_historical_order_uses_customer_part_site_and_offer(tmp_path, monkeypatch):
    monkeypatch.setattr(rules, "DB_PATH", tmp_path / "master.db")
    policy = customer.load(replay_gate.ROOT / "examples" / "kestrel" / "business.json")
    offer = {"supplier_id": "MOLD", "material_id": "RB-SHELL", "qty": 400,
             "unit_price_eur": 3, "valid_until": "2026-12-12", "transport": "Road from Antwerp to Rotterdam, 3 h"}
    with sqlite3.connect(rules.DB_PATH) as db:
        db.executescript(ingest.SCHEMA)
        db.execute("INSERT INTO components(component_id,description) VALUES ('RB-SHELL','Robot shell')")
        db.execute("INSERT INTO doc_facts VALUES ('rush.txt','offer',?,'fixture')", (json.dumps(offer),))
    sent_at = dt.datetime.fromisoformat("2026-12-11T16:00:00+01:00")
    message = replay_gate.email("Mold", "orders@mold.example", "We hereby order 400 robot shells at a total price of EUR 1200 "
                                "FCA NLRTM. Please deliver on Friday 11 December no later than 22:00.")
    action = {"id": "ACT-KESTREL", "finding_id": "offer-MOLD-RB-SHELL", "kind": "order", "payload": {},
              "messages": [dict(message, status="sent", sent_at=sent_at.isoformat())]}
    with rules.replay(policy, sent_at):
        terms = release.delivery_terms(action)
        assert terms["part"] == "RB-SHELL" and terms["supplier_id"] == "MOLD"
        assert terms["quantity"] == 400 and terms["total_price"] == 1200 and terms["unit_price"] == 3
        assert terms["delivery_place"] == "Rotterdam" and terms["incoterm"] == "FCA"
        assert terms["deliver_by"] == "2026-12-11T22:00:00+01:00"
        assert terms["depart_by"] == "2026-12-12T01:00:00+01:00"
