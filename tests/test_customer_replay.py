import datetime as dt
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

import pytest

from core import actions, customer, dispatch, ingest, outbound, rules
from examples.run_customer import run_customer


ROOT = Path(__file__).resolve().parents[1]


def test_kestrel_replays_verified_receipt_and_holds_conflicting_inbound(monkeypatch):
    def unexpected_action_db():
        pytest.fail("An offline replay must not open the action database")

    monkeypatch.setattr(actions, "con", unexpected_action_db)
    reports = run_customer(ROOT / "examples" / "kestrel")

    assert [r["build"]["issues"] for r in reports] == [0, 0, 0]
    assert [len(r["changed"]) for r in reports] == [17, 3, 1]
    assert [[e["short"] for e in r["dispatch"]["editions"]] for r in reports] == [[30, 100], [0, 30], [0, 30]]
    assert [[e["refundsAtRisk"] for e in r["dispatch"]["editions"]] for r in reports] == [[960, 1500], [0, 450], [0, 450]]
    assert all(r["dispatch"]["siteName"] == "Rotterdam" for r in reports)
    assert all(r["dispatch"]["deadline"] == "2026-12-12T06:00:00+01:00" for r in reports)
    assert all(r["dispatch"]["focusComponentId"] == "RB-MOTOR" for r in reports)
    assert reports[0]["evaluation"]["picture"]["KIT-RBT-DLX"]["packed_at_stock_snapshot"] == 20

    inbound = [next(s for s in r["evaluation"]["inbound"] if s["reference"] == "RD-201") for r in reports]
    assert [s["makes_cutoff"] for s in inbound] == [False, True, None]
    assert [s["quantity"] for s in inbound] == [30, 30, None]
    assert any("Signed handover" in flag for s in reports[0]["evaluation"]["inbound"] for flag in s["flags"])
    assert any("450 in stock can't be used" in f["title"] for f in reports[0]["evaluation"]["findings"])

    review = next(f for f in reports[2]["evaluation"]["findings"] if f["kind"] == "review")
    assert review["action"] is None
    assert {c["source"]: c["value"] for c in inbound[2]["quantity_review"]} == {
        "delivery/truck-board.csv": "30",
        "delivery/carrier-manifest.csv": "12",
    }
    motor = next(p for p in reports[2]["dispatch"]["components"] if p["id"] == "RB-MOTOR")
    assert motor["next_step"]["state"] == "Needs you"
    assert motor["next_step"]["reviewRequired"] is True


def test_held_shipment_commitment_cannot_leave_the_outbox(tmp_path, monkeypatch):
    db = tmp_path / "master.db"
    with closing(sqlite3.connect(db)) as con, con:
        con.executescript(ingest.SCHEMA)
        con.execute("INSERT INTO shipments(shipment_id,po_numbers) VALUES ('LOAD-2','KT-103')")
        con.executemany("INSERT INTO observations(entity,entity_id,field,value,source) VALUES ('shipment','LOAD-2','quantity',?,?)",
                        [("30", "truck-board.csv"), ("12", "carrier-manifest.csv")])
    monkeypatch.setattr(rules, "DB_PATH", db)
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    action = actions.propose("gap-RB-MOTOR", "Book a truck", "Protect delivery", "book_truck", 0, 0, 0, None, None,
                             messages=[{"channel": "email", "to_name": "Carrier", "to_address": "carrier@example.test",
                                        "language": "en", "subject": "Booking", "body": "Please book", "po_numbers": ["KT-103"]}])

    def unexpected_send(*args, **kwargs):
        pytest.fail("A held shipment commitment must not be sent")

    monkeypatch.setattr(outbound, "send", unexpected_send)
    assert outbound.dispatch(action["id"])["messages"][0]["status"] == "draft"
    assert rules.shipment_commitment_blocker("ship-LOAD-2", "dock_slot")
    assert rules.shipment_commitment_blocker("ship-LOAD-2", "ask") is None
    with closing(sqlite3.connect(db)) as con, con:
        con.execute("UPDATE observations SET value='30' WHERE source='carrier-manifest.csv'")
    assert rules.shipment_commitment_blocker("ship-LOAD-2", "dock_slot") is None


def test_soony_mapping_rebuild_preserves_master_rows_and_dispatch(tmp_path):
    baseline = ROOT / "data" / "soony.db"
    if not baseline.is_file() or not (ROOT / "data" / "current").is_dir():
        pytest.skip("Soony source snapshot is not in this checkout")

    scratch = tmp_path / "soony.db"
    at = dt.datetime.fromisoformat("2026-11-21T18:00:00+01:00")
    with rules.replay(customer.load(ROOT / "config" / "business.json"), at):
        assert ingest.build(scratch, ROOT / "data" / "current")["issues"] == 0
        with closing(sqlite3.connect(scratch)) as out, closing(sqlite3.connect(f"{baseline.as_uri()}?mode=ro", uri=True)) as live:
            for table in ("products", "components", "bom", "suppliers", "purchase_orders", "goods_receipts",
                          "stock", "shipments", "launch_status", "observations", "documents", "doc_facts"):
                sql = f"SELECT * FROM {table}"
                assert Counter(out.execute(sql)) == Counter(live.execute(sql)), table

        base, rebuilt = (dispatch.snapshot(None, db_path=db, include_activity=False) for db in (baseline, scratch))
        assert rebuilt["editions"] == base["editions"]
        assert rebuilt["focusComponentId"] == base["focusComponentId"] == "CMP-GAM-008"
        assert [e["short"] for e in rebuilt["editions"]] == [5900, 34800]
        offer = next(p["option"] for p in rebuilt["components"] if p["id"] == rebuilt["focusComponentId"])
        assert (offer["copiesAdded"], offer["refundsAvoided"]) == (2500, 300000)
