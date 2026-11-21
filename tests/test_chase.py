import copy
import datetime as dt
import json
import sqlite3
from collections import Counter
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from core import actions, chase, chaser, inbox, ingest, outbound, rules


@pytest.fixture
def world(tmp_path, monkeypatch):
    master = tmp_path / "master.db"
    monkeypatch.setattr(rules, "DB_PATH", master)
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    monkeypatch.setattr(rules, "CONFIG", copy.deepcopy(rules.CONFIG))
    settings = rules.cfg("chase")
    settings.update(enabled=True, internal_emails={"Mei Lin": "buyer@example.test"})
    monkeypatch.setenv("SOONY_NOW", "2026-11-21T12:00:00+01:00")
    with sqlite3.connect(master) as db:
        db.executescript(ingest.SCHEMA)
        db.executemany("INSERT INTO products(product_id,name,launch_date) VALUES (?,?,?)",
                       [(rules.cfg("priority_product"), "Collector's Edition", "2026-11-22"),
                        ("STANDARD", "Standard Edition", "2026-11-22"), ("LEGACY", "Previous edition", "2020-01-01")])
        db.executemany("INSERT INTO components(component_id,description,category,drawing_rev) VALUES (?,?,?,?)",
                       [("M1", "Art Book", "Collector's Items", "B"), ("M2", "Cover Sleeve", "Print & Packaging", "B"),
                        ("M3", "Game Card", "Game Media", "A")])
        db.executemany("INSERT INTO bom(parent_id,child_id,qty_per) VALUES (?,?,1)",
                       [(rules.cfg("priority_product"), "M1"), ("STANDARD", "M2"), ("LEGACY", "M3")])

    sent = []

    def send(message, parent=None):
        mid = f"<{message['id']}@ours.example.test>"
        sent.append((message, parent, mid))
        outbound._mark(message["id"], "sent", mid)
        return "sent"

    monkeypatch.setattr(outbound, "send", send)

    def order(po, *, supplier="S1", line=1, material="M1", requested="2026-11-21", promised=None, qty=1200):
        with sqlite3.connect(master) as db:
            db.execute("INSERT OR IGNORE INTO suppliers(supplier_id,name,contact_email,language) VALUES (?,?,?,?)",
                       (supplier, "Supplier " + supplier, f"{supplier.lower()}@example.test", "en"))
            db.execute("""INSERT INTO purchase_orders(po_number,po_line,supplier_id,material_id,plant,quantity,order_date,
                       requested_date,promised_date,status,source) VALUES (?,?,?,?,?,?,'2026-10-01',?,?,'open','soony/erp/PO.csv')""",
                       (po, line, supplier, material, rules.CONFIG["site"], qty, requested, promised))

    def doc(po, *, filename="OA90", supplier="S1", header="Order Confirmation", rows=(), body=None):
        path = f"suppliers/{supplier}_Supplier/order_confirmations/{filename}_{po}.pdf"
        text = f"{header}\nCustomer: Soony · Your order {po}\n\n" + "\n".join(rows) if body is None else body
        with sqlite3.connect(master) as db:
            db.execute("INSERT INTO documents VALUES (?,?,?,?,?)", (path, supplier, "order_confirmations", po, text))

    def fact(po, kind, *, material="M1", supplier="S1", **values):
        with sqlite3.connect(master) as db:
            db.execute("INSERT INTO doc_facts VALUES (?,?,?,?)", (f"suppliers/{supplier}_Supplier/{kind}_{po}.pdf", kind,
                       json.dumps({"po_number": po, "material_id": material, "quote": f"Supplier {kind} evidence", **values}), "test"))

    def advance(hours):
        monkeypatch.setenv("SOONY_NOW", (rules.now() + dt.timedelta(hours=hours)).isoformat())

    return SimpleNamespace(order=order, doc=doc, fact=fact, advance=advance, master=master, sent=sent, settings=settings)


def row(num="00010", material="M1", description="Art Book", qty="1,200", date="2026-11-21"):
    return f" {num}    {material}    {description}    {qty}    5.00    EUR    {date}"


def test_detection_uses_line_evidence_and_launch_scope(world):
    for po in ("0101", "0102", "0103", "0104", "0105", "0106", "0107"):
        world.order(po, promised="2026-11-20" if po == "0102" else None)
    world.doc("0102", header="Amended Order Confirmation", rows=[row(qty="900", date="2026-11-23")])
    world.fact("0102", "delivery_promise", qty=1000, arrival_date="2026-11-24")
    world.fact("0102", "artwork_revision", revision="A")
    world.doc("0103", body="unreadable PDF")
    world.doc("0104", rows=[row()])
    world.fact("0104", "shipping_advice", qty_ordered=1200, qty_shipped=800, balance=400, balance_eta="Hamburg 2026-11-24")
    world.doc("0105", rows=[row()])
    world.fact("0105", "receipt_correction", qty_typed=12000, qty_corrected=1200)
    world.doc("0106", rows=[row()])
    world.fact("0106", "delivery_promise", qty=1200, dispatch_date="2026-11-20")
    world.doc("0107", rows=[row(qty="unknown")])
    world.order("0108", requested=None)
    world.order("0998", requested="2026-11-23")
    world.order("0999", material="M3")

    report = chase.run(dry_run=True)
    results = {}
    for case in report["opened"]:
        results.setdefault(case["po_number"], set()).add(case["kind"])
    assert results == {
        "0101": {"missing_confirmation"},
        "0102": {"amended_confirmation", "partial_quantity", "late_date", "contradicting_qty", "contradicting_date", "contradicting_revision"},
        "0103": {"needs_review"},
        "0104": {"partial_quantity", "late_date"},
        "0105": {"contradicting_qty"},
        "0107": {"needs_review"},
        "0108": {"needs_review"},
    }
    assert {m["po_number"] for m in report["mail"]} == {"0101", "0102", "0104"}
    assert not actions.DB.exists() and not world.sent


def test_line_alias_requires_supplier_catalog_and_matching_line(world):
    world.order("0101")
    world.order("0101", line=2, material="M2")
    with sqlite3.connect(world.master) as db:
        db.execute("INSERT INTO supplier_components(supplier_id,component_id,description) VALUES ('S1','M1','Art Book')")
    world.doc("0101", rows=[row(material="LOCAL-123"), row(num="00020", material="LOCAL-456", description="Other part")])
    report = chase.run(dry_run=True)
    assert [(r["po_line"], r["kind"]) for r in report["opened"]] == [(2, "needs_review")]
    assert not report["mail"]


def test_stable_keys_group_cases_and_cap_new_threads_with_priority(world):
    world.settings["max_new_threads_per_cycle"] = 2
    world.order("0101", material="M2", requested="2026-11-19")
    world.order("0102", requested="2026-11-20")
    world.order("0102", line=2, material="M2", requested="2026-11-21")
    world.order("0103", supplier="S2")
    simulation = chase.run(dry_run=True)
    assert [m["po_number"] for m in simulation["mail"]] == ["0102", "0103"]
    report = chase.run()
    assert {r["case_id"] for r in report["opened"]} == {r["case_id"] for r in simulation["opened"]}
    assert len(world.sent) == 2
    first, _, _ = world.sent[0]
    assert "Art Book" in first["body"] and "Cover Sleeve" in first["body"]
    assert "M1" not in first["body"] and "Could you confirm" in first["body"]
    assert {field: first[field] for field in ("to_address", "language", "subject", "body")} == {
        field: simulation["mail"][0][field] for field in ("to_address", "language", "subject", "body")
    }
    assert chase.run()["opened"] == []
    assert len(world.sent) == 3
    assert not chase.run()["mail"]
    assert len(world.sent) == 3


def test_one_threaded_reminder_then_internal_escalation(world):
    world.order("0101")
    initial = chase.run()["opened"][0]["case_id"]
    assert chaser.detail(initial)["status"] == "waiting"
    world.advance(1)
    assert not chase.run()["mail"]
    world.advance(1)
    assert chase.run()["mail"][0]["kind"] == "reminder"
    assert world.sent[1][1] == world.sent[0][2]
    assert world.sent[1][0]["subject"] == world.sent[0][0]["subject"]
    world.advance(2)
    assert chase.run()["escalations"] == [{"po_number": "0101", "owner": "Mei Lin", "email": True}]
    assert world.sent[2][0]["to_address"] == "buyer@example.test"
    assert chaser.detail(initial)["status"] == "escalated"
    world.advance(2)
    assert not chase.run()["mail"]
    assert len(world.sent) == 3


def inbound(world, text, subject="Re: Hamburg delivery", sender="s1@example.test", parent=None, imap_id=1):
    _message, _, sent_id = world.sent[0]
    email = EmailMessage()
    email["Message-ID"] = f"<supplier-{imap_id}@example.test>"
    email["From"], email["Subject"] = sender, subject
    email["In-Reply-To"] = parent or sent_id
    email.set_content(text)

    class Mailbox:
        def login(self, user, password):
            assert (user, password) == ("test", "fixture")

        def select(self, name, readonly):
            assert readonly is True

        def search(self, *args):
            return "OK", [b"1"]

        def fetch(self, number, fields):
            assert fields in {"(BODY.PEEK[HEADER])", "(BODY.PEEK[])"}
            data = email.as_bytes()
            return "OK", [(b"message", data.split(b"\n\n", 1)[0] if fields == "(BODY.PEEK[HEADER])" else data)]

        def logout(self):
            pass

    return Mailbox()


def test_correlated_reply_resolves_and_deduplicates(world, monkeypatch):
    world.order("0101")
    cid = chase.run()["opened"][0]["case_id"]
    text = "We confirm 1,200 art books will arrive in Hamburg by 2026-11-21."
    monkeypatch.setenv("IMAP_HOST", "fixture")
    monkeypatch.setenv("IMAP_USER", "test")
    monkeypatch.setenv("IMAP_PASSWORD", "fixture")
    wrong = inbound(world, text, parent="<unrelated@example.test>")
    old = inbound(world, text, subject="Re: [SC:fixture] Hamburg delivery")
    impostor = inbound(world, text, sender="other@example.test")
    for mailbox in (wrong, old, impostor):
        monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", lambda *args, **kw: mailbox)
        assert not inbox.fetch()
    valid = inbound(world, text)
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", lambda *args, **kw: valid)
    assert len(inbox.fetch()) == 1 and inbox.fetch() == []
    assert inbox.unhandled() == []
    assert chase.run()["replies_processed"] == 1
    case = chaser.detail(cid)
    assert case["status"] == "resolved"
    assert case["last_reply_summary"] == "Supplier made an explicit delivery confirmation."
    assert {m["direction"] for m in case["messages"]} == {"inbound", "outbound"}
    assert case["events"][-1]["evidence"][0]["quote"] == text
    assert not chase.run()["replies_processed"]


@pytest.mark.parametrize("reply", [
    "We can deliver 900 art books on 2026-11-23.",
    "We do not confirm 1,200 art books will arrive in Hamburg by 2026-11-21.",
    "We confirm on 2026-11-21 that 1,200 art books will arrive in Hamburg.",
    "We confirm 1,200 art books will arrive in Hamburg by 2026-11-21 if the carrier accepts the booking.",
    "Please confirm that we can deliver 1,200 art books to Hamburg by 2026-11-21.",
])
def test_unsettled_reply_opens_internal_follow_up(world, monkeypatch, reply):
    world.order("0101")
    case = chase.run()["opened"][0]
    monkeypatch.setenv("IMAP_HOST", "fixture")
    monkeypatch.setenv("IMAP_USER", "test")
    monkeypatch.setenv("IMAP_PASSWORD", "fixture")
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", lambda *args, **kw: inbound(world, reply))
    inbox.fetch()
    preview = chase.run(dry_run=True)
    chase.run()
    assert chaser.detail(case["case_id"])["status"] == "escalated"
    follow_up = next(c for c in chaser.list_native() if c["issue_kind"] == "reply_follow_up")
    assert follow_up["needs_review"]
    assert world.sent[1][0]["body"] == preview["escalation_messages"][0]["body"]
    world.advance(4)
    assert not chase.run()["mail"]
    assert Counter(m[0]["to_address"] for m in world.sent) == {"s1@example.test": 1, "buyer@example.test": 1}


def test_attaches_to_existing_agent_action(world):
    world.order("0101")
    action = actions.propose("agent-confirm", "Confirm art books", "Verify arrival.", "ask", 0, 0, 0, None, "Dockside",
                             messages=[{"channel": "email", "to_name": "Supplier", "to_address": "s1@example.test",
                                        "language": "en", "subject": "Delivery", "body": "Please confirm arrival.", "po_numbers": ["0101"]},
                                       {"channel": "email", "to_name": "Supplier", "to_address": "s1@example.test",
                                        "language": "en", "subject": "Other delivery", "body": "Any news?", "po_numbers": ["0102"]}])
    report = chase.run()
    assert not report["mail"] and not world.sent
    assert report["attached"][0]["action_id"] == action["id"]
    assert chaser.detail(report["opened"][0]["case_id"])["messages"][0]["id"] == action["messages"][0]["id"]
    outbound.dispatch(action["id"])
    world.advance(4)
    assert not chase.run()["mail"]
    assert Counter(m[0]["to_address"] for m in world.sent) == {"s1@example.test": 2, "buyer@example.test": 1}


def test_recent_legacy_followup_is_reused_but_old_mail_is_not(world):
    for po in ("0101", "0102"):
        world.order(po)
    with sqlite3.connect(world.master) as db:
        db.execute("INSERT INTO shipments(shipment_id,po_numbers) VALUES ('SHIPPING-1','0101')")
    mail = {"channel": "email", "to_name": "Supplier", "to_address": "s1@example.test",
            "language": "en", "subject": "Delivery", "body": "Please confirm arrival."}
    old = actions.propose("old", "Confirm old order", "Verify arrival.", "ask", 0, 0, 0, None, "Dockside",
                          messages=[dict(mail, po_numbers=["0102"])])
    outbound.dispatch(old["id"])
    world.advance(3)
    followup = actions.propose("followup-ship-SHIPPING-1-202611211500", "Check delivery", "Verify arrival.", "ask",
                               0, 0, 0, None, "Dockside", messages=[mail])
    outbound.dispatch(followup["id"])

    report = chase.run()
    assert [a["action_id"] for a in report["attached"]] == [followup["id"]]
    assert [m["po_number"] for m in report["mail"]] == ["0102"]


def test_outbound_blocks_duplicate_followups_with_explicit_and_legacy_po_scope(world):
    world.order("0101")
    with sqlite3.connect(world.master) as db:
        db.execute("INSERT INTO shipments(shipment_id,po_numbers) VALUES ('SHIPPING-1','0101')")
    chase.run()
    mail = {"channel": "email", "to_name": "Supplier", "to_address": "s1@example.test",
            "language": "en", "subject": "Delivery", "body": "Please confirm arrival."}
    duplicate = actions.propose("followup-ship-SHIPPING-1-202611211200", "Check delivery", "Verify arrival.",
                                 "ask", 0, 0, 0, None, "Dockside",
                                 messages=[dict(mail, po_numbers=["0101"]), mail, dict(mail, po_numbers=["0102"])])
    released = outbound.dispatch(duplicate["id"])
    assert Counter(m["status"] for m in released["messages"]) == {"cancelled": 2, "sent": 1}
    assert len(world.sent) == 2


def test_confirmation_chase_skips_covered_and_already_shipped_lines(world):
    for po in ("0101", "0102", "0103", "0104"):
        world.order(po, promised="2026-11-23" if po in {"0101", "0102"} else "2026-11-21")
    world.doc("0101", rows=[row(date="2026-11-23")])
    world.doc("0102", rows=[row(date="2026-11-23", qty="900")])
    with sqlite3.connect(world.master) as db:
        db.executemany("""INSERT INTO shipments(shipment_id,supplier_id,po_numbers,material_ids,status,eta,quantity)
                           VALUES (?,'S1',?,'M1','in_transit','2026-11-24',1200)""",
                       [("SHIPPING-1", "0101"), ("SHIPPING-2", "0102")])
        db.execute("""INSERT INTO goods_receipts(gr_number,po_number,po_line,supplier_id,material_id,qty_ordered,qty_received)
                       VALUES ('GR1','0103',1,'S1','M1',1200,1200)""")
    report = chase.run(dry_run=True)
    assert {r["po_number"] for r in report["opened"]} == {"0102", "0104"}
    assert {m["po_number"] for m in report["mail"]} == {"0102", "0104"}


def test_missing_internal_email_queues_discord_escalation(world):
    world.settings["internal_emails"] = {}
    world.order("0101")
    chase.run()
    world.advance(2)
    chase.run()
    world.advance(2)
    preview = chase.run(dry_run=True)
    assert preview["escalation_messages"][0]["channel"] == "discord"
    assert preview["escalation_messages"][0]["to_name"] == "Mei Lin"
    report = chase.run()
    assert report["escalations"] == [{"po_number": "0101", "owner": "Mei Lin", "email": False}]
    assert len(world.sent) == 2
    aid = report["actions"][0]
    assert outbound.dispatch(aid)["messages"][0]["status"] == "draft"
    assert chase.run()["actions"] == [aid]


def test_disabled_cycle_never_dispatches_queued_mail(world, monkeypatch):
    world.order("0101")
    send = outbound.send
    monkeypatch.setattr(outbound, "send", lambda message, parent=None: outbound._mark(message["id"], "outbox") or "outbox")
    chase.run()
    with actions.con() as c:
        aid = c.execute("SELECT initial_action_id FROM chase_threads").fetchone()[0]
    assert aid not in actions.pending_dispatch()
    world.settings["enabled"] = False
    monkeypatch.setattr(outbound, "send", send)
    chase.run()
    outbound.dispatch(aid)
    assert not world.sent
    world.settings["enabled"] = True
    # An existing queued message is dispatched exactly once when re-enabled.
    chase.run()
    chase.run()
    assert len(world.sent) == 1


def test_uncertain_delivery_is_never_sent_again(world, monkeypatch):
    world.order("0101")
    normal_send = outbound.send
    attempts = []

    def unknown(message, parent=None):
        attempts.append(message["id"])
        outbound._mark(message["id"], "delivery_unknown", "<uncertain@example.test>")
        return "delivery_unknown"

    monkeypatch.setattr(outbound, "send", unknown)
    cid = chase.run()["opened"][0]["case_id"]
    assert len(attempts) == 1
    monkeypatch.setattr(outbound, "send", normal_send)
    world.advance(2)
    assert not chase.run()["mail"]
    assert chaser.detail(cid)["status"] == "escalated"
    world.advance(2)
    chase.run()
    assert [m[0]["to_address"] for m in world.sent] == ["buyer@example.test"]


def test_draft_works_without_openai(world, monkeypatch):
    world.order("0101")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    chase.run()
    assert len(world.sent) == 1
    assert world.sent[0][0]["body"].startswith("Could you confirm the following")


def test_new_confirmation_resolves_case_and_cancels_outbox(world, monkeypatch):
    world.order("0101")
    normal_send = outbound.send

    def outbox(message, parent=None):
        outbound._mark(message["id"], "outbox")
        return "outbox"

    monkeypatch.setattr(outbound, "send", outbox)
    cid = chase.run()["opened"][0]["case_id"]
    assert len(chase.run(dry_run=True)["queued_mail"]) == 1
    world.doc("0101", rows=[row()])
    monkeypatch.setattr(outbound, "send", normal_send)
    chase.run()
    case = chaser.detail(cid)
    assert not world.sent
    assert case["status"] == "resolved" and case["messages"][0]["status"] == "cancelled"
    assert any(event["kind"] == "resolved" for event in case["events"])
