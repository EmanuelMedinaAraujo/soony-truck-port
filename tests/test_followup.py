import datetime as dt
from unittest import mock

from core import followup, rules

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
