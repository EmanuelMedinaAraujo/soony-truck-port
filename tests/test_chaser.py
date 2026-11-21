import json
import sqlite3

import pytest

from core import actions, agent, chaser, outbound, rules


@pytest.fixture
def native_cases(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    with actions.con() as c:
        c.executemany("""INSERT INTO chase_cases
            (case_id,supplier_id,supplier_name,po_number,po_line,material,issue_kind,status,question,
             needs_review,created_at,updated_at,last_reply_summary)
            VALUES (?,'S001','Northstar',?,?,'card','partial_quantity',?,'Can you confirm 1,200 units?',0,
                    '2026-11-21T12:00:00Z','2026-11-21T12:00:00Z','We can provide 900 units.')""",
                      [("case-1", "000007", 1, "waiting"), ("case-2", "000007", 2, "resolved"),
                       ("case-3", "000008", 1, "escalated")])
    return tmp_path


def test_native_summary_keeps_existing_api_fields(native_cases):
    rows, status = chaser.cases(supplier_id="S001", po_number="000007", active_only=True)
    assert status == "ok"
    assert rows == [{"case_id": "case-1", "supplier_id": "S001", "supplier_name": "Northstar",
                     "po_number": "000007", "material": "card", "status": "waiting",
                     "question": "Can you confirm 1,200 units?", "last_reply_summary": "We can provide 900 units.",
                     "updated_at": "2026-11-21T12:00:00Z"}]
    rows, _ = chaser.cases(include_messages=False)
    assert [(r["status"], r["question"], r["last_reply_summary"]) for r in rows] == [
        ("waiting", None, None), ("closed", None, None), ("escalated", None, None)]
    assert chaser.detail("case-2")["status"] == "resolved"
    assert chaser.detail("no-such-case") is None


def test_duplicate_email_is_removed_and_case_is_logged(native_cases, monkeypatch):
    master_db = native_cases / "master.db"
    with sqlite3.connect(master_db) as conn:
        conn.execute("CREATE TABLE suppliers(supplier_id TEXT, contact_email TEXT)")
        conn.executemany("INSERT INTO suppliers VALUES (?, ?)",
                         [("S001", "one@example.test"), ("S002", "two@example.test")])
    monkeypatch.setattr(rules, "DB_PATH", master_db)
    monkeypatch.setattr(outbound, "dispatch", lambda action_id: None)
    message = {"channel": "email", "to_name": "Supplier", "language": "en", "subject": "Confirm delivery",
               "body": "Can you confirm the delivery date?", "body_en": None, "po_numbers": ["000007"]}
    assert agent.Tools().get_chaser_cases("S001", "000007")["cases"][0]["case_id"] == "case-1"
    result = agent.Tools().propose_action(
        "delivery-date", "Check the delivery", "Confirm the delivery date.", "PO reference checked.",
        "ask", 0, 0, 0, None, "Dockside", 60,
        [dict(message, to_address="ONE@example.test"), dict(message, to_address="two@example.test")],
    )
    recorded = actions.get(result["done_automatically"])
    assert [m["to_address"] for m in recorded["messages"]] == ["two@example.test"]
    assert "case-1" in recorded["payload"]["details"]
    assert json.loads(recorded["messages"][0]["po_numbers"]) == ["000007"]
