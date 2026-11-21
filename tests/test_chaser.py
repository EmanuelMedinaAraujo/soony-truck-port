import sqlite3

import httpx
import pytest

from core import actions, agent, chaser, outbound, rules


@pytest.fixture
def stub_chaser(monkeypatch):
    monkeypatch.setenv("CHASER_URL", "http://127.0.0.1:8000/")
    monkeypatch.setenv("CHASER_TOKEN", "test-token")
    client = httpx.Client

    def install(handler):
        def request(req):
            assert req.headers["Authorization"] == "Bearer test-token"
            return handler(req)

        monkeypatch.setattr(chaser.httpx, "Client", lambda **kwargs: client(
            transport=httpx.MockTransport(request), **kwargs))

    return install


def upstream_case(case_id="case-1", state="AWAITING_REPLY", po_number="000007"):
    return {"id": case_id, "state": state, "supplier_id": "S001", "supplier": "Northstar",
            "po": {"po_number": po_number, "part_number": "card"}, "updated_at": "2026-11-21T12:00:00Z"}


def test_normalizes_authentic_case_and_message_fields(stub_chaser):
    def request(req):
        if req.url.path == "/api/v1/cases":
            assert req.url.params["supplier_id"] == "S001"
            return httpx.Response(200, json=[upstream_case(), upstream_case("case-2", "RESOLVED"),
                                             upstream_case("case-3", po_number="000008")])
        assert req.url.path == "/api/v1/cases/case-1"
        return httpx.Response(200, json={"messages": [
            {"direction": "outbound", "status": "superseded", "body": "Old draft", "at": "2026-11-21T12:04:00Z"},
            {"direction": "inbound", "body": "We can provide\n  900 units.", "at": "2026-11-21T12:03:00Z"},
            {"direction": "outbound", "status": "sent", "body": "Can you confirm 1,200 units?", "at": "2026-11-21T12:02:00Z"},
        ]})

    stub_chaser(request)
    rows, status = chaser.cases(supplier_id="S001", po_number="000007", active_only=True)
    assert status == "ok"
    assert rows == [{"case_id": "case-1", "supplier_id": "S001", "supplier_name": "Northstar",
                     "po_number": "000007", "material": "card", "status": "waiting",
                     "question": "Can you confirm 1,200 units?", "last_reply_summary": "We can provide 900 units.",
                     "updated_at": "2026-11-21T12:00:00Z", "url": "http://127.0.0.1:8000/cases/case-1"}]


@pytest.mark.parametrize(("upstream", "normalized"), [
    ("NEW", "open"), ("RECONCILING", "answered"), ("BLOCKED", "escalated"), ("CANCELLED", "closed"),
])
def test_list_only_normalizes_states_without_loading_details(stub_chaser, upstream, normalized):
    def request(req):
        assert req.url.path == "/api/v1/cases"
        return httpx.Response(200, json=[upstream_case(state=upstream)])

    stub_chaser(request)
    rows, status = chaser.cases(include_messages=False)
    assert status == "ok"
    assert rows[0]["status"] == normalized
    assert rows[0]["question"] is None


def test_service_down(stub_chaser):
    def request(req):
        raise httpx.ConnectError("connection unavailable", request=req)

    stub_chaser(request)
    assert chaser.cases() == ([], "chaser offline")


def test_failed_case_detail_preserves_the_case(stub_chaser):
    def request(req):
        if req.url.path == "/api/v1/cases":
            return httpx.Response(200, json=[upstream_case()])
        return httpx.Response(503)

    stub_chaser(request)
    rows, status = chaser.cases()
    assert status == "ok"
    assert rows[0]["case_id"] == "case-1"
    assert rows[0]["last_reply_summary"] is None


def test_duplicate_email_is_removed_and_case_is_logged(tmp_path, monkeypatch, stub_chaser):
    master_db = tmp_path / "master.db"
    with sqlite3.connect(master_db) as conn:
        conn.execute("CREATE TABLE suppliers(supplier_id TEXT, contact_email TEXT)")
        conn.executemany("INSERT INTO suppliers VALUES (?, ?)",
                         [("S001", "one@example.test"), ("S002", "two@example.test")])
    monkeypatch.setattr(rules, "DB_PATH", master_db)
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    monkeypatch.setattr(outbound, "dispatch", lambda action_id: None)
    stub_chaser(lambda request: httpx.Response(200, json=[upstream_case()]))

    message = {"channel": "email", "to_name": "Supplier", "language": "en", "subject": "Confirm delivery",
               "body": "Can you confirm the delivery date?", "body_en": None, "po_numbers": ["000007"]}
    result = agent.Tools().propose_action(
        "delivery-date", "Check the delivery", "Confirm the delivery date.", "PO reference checked.",
        "ask", 0, 0, 0, None, "Dockside", 60,
        [dict(message, to_address="ONE@example.test"), dict(message, to_address="two@example.test")],
    )
    recorded = actions.get(result["done_automatically"])
    assert [m["to_address"] for m in recorded["messages"]] == ["two@example.test"]
    assert "case-1 (http://127.0.0.1:8000/cases/case-1)" in recorded["payload"]["details"]
