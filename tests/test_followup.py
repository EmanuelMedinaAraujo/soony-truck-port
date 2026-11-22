import datetime as dt
from unittest import mock

import pytest

from core import actions, agent, customer, followup, rules
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
