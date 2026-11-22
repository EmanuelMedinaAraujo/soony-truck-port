import pytest
from fastapi.testclient import TestClient

from core import actions


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    return tmp_path


def order(finding, to="a.nowak@example.com", cost=16800):
    return actions.propose(finding, "Order 8,000 art books", "rush", "order", cost, 300000, 2500, None, None,
                           messages=[{"channel": "email", "to_name": "Agnieszka Nowak", "to_address": to, "language": "pl",
                                      "subject": "Zamówienie", "body": "8 000 szt."}])


def test_second_order_for_the_same_part_and_supplier_is_recognised(log):
    first = order("gap-CMP-GAM-008")
    twin = actions.open_commitment("order", "offer-SUP-0028-CMP-GAM-008", ["a.nowak@example.com"])
    assert twin and twin["id"] == first["id"]
    assert actions.open_commitment("order", "offer-SUP-0028-CMP-GAM-001", ["a.nowak@example.com"]) is None
    assert actions.open_commitment("order", "gap-CMP-GAM-008", ["someone@else.com"]) is None
    assert actions.open_commitment("ask", "gap-CMP-GAM-008", ["a.nowak@example.com"]) is None


def test_scheduled_commitment_is_not_released_twice(log):
    first = order("gap-CMP-GAM-008")
    actions.set_status(first["id"], "sent", "sent")
    second = order("offer-SUP-0028-CMP-GAM-008")
    assert "duplicate of " + first["id"] in (actions.release_blocker(second) or "")


def test_dashboard_decisions_need_a_verified_person(log, monkeypatch):
    from server import app as server
    a = order("gap-CMP-GAM-009")
    client = TestClient(server.app)
    forged = client.post(f"/api/actions/{a['id']}/decide", json={"decision": "reject", "by": "Paul Winter"},
                         headers={"X-Forwarded-For": "10.0.0.5"})
    assert forged.status_code == 403
    ok = client.post(f"/api/actions/{a['id']}/decide", json={"decision": "reject", "by": "Paul Winter"},
                     headers={"Tailscale-User-Name": "Katrin Jansen"})
    assert ok.status_code == 200
    assert "Katrin Jansen (dashboard)" in str(ok.json()["history"])
