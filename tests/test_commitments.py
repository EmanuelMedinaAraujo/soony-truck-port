import datetime as dt
import sys
import json
import sqlite3
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import actions, approvals, inbox, ingest, outbound, release, rules
from server import app as server

SUPPLIER = "SUP-0028"
PART = "CMP-GAM-008"
ADDRESS = "supplier@example.test"
FINDING = f"offer-{SUPPLIER}-{PART}"
PO = "4500000001"


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    monkeypatch.setattr(rules, "DB_PATH", tmp_path / "master.db")
    monkeypatch.setattr(server, "STATE", server.STATE.copy())
    monkeypatch.setenv("SOONY_NOW", "2026-11-22T10:00:00+01:00")
    for env in ("SOONY_TOKEN", "IMAP_HOST", "SMTP_HOST", "OPENAI_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    for key, value in (
        ("staff_accounts", {"paul@example.test": "Paul Winter", "katrin@example.test": "Katrin Jansen"}),
        ("staff_email", {"Paul Winter": "paul@example.test"}),
        ("release_conditions", {}),
    ):
        monkeypatch.setitem(rules.CONFIG, key, {"value": value})
    with sqlite3.connect(rules.DB_PATH) as db:
        db.executescript(ingest.SCHEMA)
        db.execute("INSERT INTO suppliers(supplier_id,status,contact_email) VALUES (?, 'active', ?)", (SUPPLIER, ADDRESS))
        db.execute("""INSERT INTO purchase_orders(po_number,supplier_id,material_id,quantity,unit_price,currency,status,
                                                  po_line,incoterm,plant,requested_date)
                      VALUES (?,?,?,8000,2.1,'EUR','open',10,'DAP','Hamburg','2026-11-23')""", (PO, SUPPLIER, PART))
    release.record_check("files")
    release.record_check("mailbox")


@pytest.fixture
def client(log):
    return TestClient(server.app, client=("127.0.0.1", 12345))


@pytest.fixture
def deliveries(log, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("MAIL_FROM", "dockside@example.test")
    class Deliveries(list):
        messages = []

    sent = Deliveries()

    class SMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def starttls(self):
            pass

        def send_message(self, message):
            sent.append(message["To"])
            sent.messages.append(message)

    monkeypatch.setattr(outbound.smtplib, "SMTP", SMTP)
    return sent


def message(to=ADDRESS, *, po=PO, body="8,000 units", subject="Delivery details"):
    return {"channel": "email", "to_name": "Supplier", "to_address": to, "language": "en",
            "subject": subject, "body": body, "po_numbers": [po] if po else []}


def order(finding=FINDING, *, to=ADDRESS, protected=1000, payload=None, parent=None, body="8,000 units"):
    terms = {"quantity": 8000, "unit_price_eur": 2.1, "valid_until": "2026-11-22", **(payload or {})}
    return actions.propose(finding, "Order 8,000 art books", "rush", "order", 16800, protected, 2500,
                           "Print & Packaging", None, payload=terms, messages=[message(to, body=body)], parent=parent)


def approve(action, source="dashboard", person="Paul Winter"):
    return server.decide_and_dispatch(action["id"], "approve", person, source=source,
                                      commitment_digest=release.approval_digest(action))


def reply_from(origin, *, handled, text="Only 4,000 are available and the price is now EUR 2.85."):
    message, = origin["messages"]
    with actions.tx() as c:
        inbox.init_schema(c)
        c.execute("""INSERT INTO replies(id,message_id,msg_id,action_id,from_addr,received_at,body,handled)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  ("reply-" + origin["id"], "<" + origin["id"] + "@example.test>", message["id"], origin["id"],
                   message["to_address"], rules.now().isoformat(), text, handled))


def test_second_order_for_the_same_part_and_supplier_is_recognised(log):
    first = order(f"gap-{PART}")
    twin = actions.open_commitment("order", FINDING, [ADDRESS])
    assert twin and twin["id"] == first["id"]
    assert actions.open_commitment("order", f"offer-{SUPPLIER}-CMP-GAM-001", [ADDRESS]) is None
    assert actions.open_commitment("order", f"gap-{PART}", ["someone@else.com"]) is None
    assert actions.open_commitment("ask", f"gap-{PART}", [ADDRESS]) is None


def test_dashboard_rejects_wrong_approver_and_display_name(client):
    action = order()
    path = f"/api/actions/{action['id']}/decide"
    body = {"decision": "reject", "by": "Paul Winter"}
    forged = client.post(path, json=body, headers={"Tailscale-User-Name": "Paul Winter"})
    wrong = client.post(path, json=body, headers={"Tailscale-User-Login": "katrin@example.test"})
    assert forged.status_code == wrong.status_code == 403
    assert actions.get(action["id"])["status"] == "proposed"
    result = client.post(path, json=body, headers={"Tailscale-User-Login": "paul@example.test"})
    assert result.status_code == 200
    assert "Paul Winter (dashboard)" in str(result.json()["history"])


def test_local_operator_requires_explicit_configuration(client, monkeypatch):
    action = order()
    path = f"/api/actions/{action['id']}/decide"
    assert client.post(path, json={"decision": "reject"}).status_code == 403
    monkeypatch.setitem(rules.CONFIG, "demo_local_operator", {"value": "Paul Winter"})
    assert client.post(path, json={"decision": "reject"}, headers={"X-Forwarded-For": "10.0.0.5"}).status_code == 403
    assert client.post(path, json={"decision": "reject"}).status_code == 200


def test_dashboard_holds_newer_supplier_reply_on_approval_and_retry(client, deliveries, monkeypatch):
    action = order()
    monkeypatch.setenv("SOONY_NOW", "2026-11-22T10:02:00+01:00")
    origin = actions.propose(FINDING, "Check terms", "", "ask", 0, 0, 0, None, None, messages=[message()])
    outbound._mark(origin["messages"][0]["id"], "sent")
    reply_from(origin, handled=True)
    result = client.post(f"/api/actions/{action['id']}/decide", json={"decision": "approve", "commitment_digest": release.approval_digest(action)},
                         headers={"Tailscale-User-Login": "paul@example.test"})
    held = result.json()
    assert result.status_code == 200 and held["status"] == "approved"
    assert "newer supplier reply" in held["held_reason"]
    assert held["signatures"]["Paul Winter"]["source"] == "dashboard"
    assert outbound.dispatch(action["id"])["held_reason"] == held["held_reason"]
    assert deliveries == []
    with actions.tx() as c:
        assert c.execute("SELECT ref FROM events WHERE kind='release_held'").fetchall()[0][0] == action["id"]
        assert c.execute("SELECT count(*) FROM events WHERE kind='release_held'").fetchone()[0] == 1
    monkeypatch.setattr(server.dispatch, "snapshot", lambda _: {"decisions": []})
    decision, = client.get("/api/dispatch").json()["decisions"]
    assert decision["held_reason"] == held["held_reason"]
    assert decision["commitment_id"] == action["commitment_id"]


def test_outbox_retry_rechecks_updated_po(log, deliveries, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    action = order()
    assert approve(action)["messages"][0]["status"] == "outbox"
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    with sqlite3.connect(rules.DB_PATH) as db:
        db.execute("UPDATE purchase_orders SET unit_price=2.85")
    held = outbound.dispatch(action["id"])
    assert held["status"] == "approved" and "price" in held["held_reason"]
    assert held["commitment_id"] == action["commitment_id"]
    assert deliveries == []


def test_expires_and_requires_fresh_checks(log, deliveries, monkeypatch):
    expired = order(payload={"latestUsefulAt": "2026-11-22T09:59:00+01:00"})
    with pytest.raises(ValueError, match="expired"):
        approve(expired)
    assert release.decision_state(actions.get(expired["id"]))["terminal"] == "Expired — no longer useful"
    action = order()
    with actions.tx() as c:
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        c.execute("UPDATE release_evidence SET checked_at=? WHERE source='files'", (old.isoformat(),))
    assert approve(action)["held_reason"] == "evidence stale"
    release.record_check("files")
    released = outbound.dispatch(action["id"])
    assert released["status"] == "sent" and released["held_reason"] is None
    assert deliveries == [ADDRESS]


def test_same_commitment_key_is_superseded_even_on_reply_follow_up(log, deliveries):
    first = approve(order())
    second = order("reply-new", parent=first)
    assert second["commitment_id"] == first["commitment_id"]
    held = approve(second)
    assert held["status"] == "superseded"
    assert first["id"] in held["held_reason"]
    assert deliveries == [ADDRESS]


def test_joint_signatures_from_two_channels(log, deliveries, monkeypatch):
    monkeypatch.setitem(rules.CONFIG, "approval_by_kind",
                        {"value": {"order": ["Site Lead DC-HAM (dock slots)"]}})
    action = order(protected=300000)
    assert action["status"] == "proposed"
    assert actions.signers(action) == ("Katrin Jansen", "Paul Winter")
    first = approve(action, source="Discord", person="Katrin Jansen")
    assert first["status"] == "proposed" and deliveries == []
    assert set(first["signatures"]) == {"Katrin Jansen"}
    with pytest.raises(ValueError, match="already signed"):
        approve(action, person="Katrin Jansen")
    released = approve(action)
    assert set(released["signatures"]) == {"Katrin Jansen", "Paul Winter"}
    assert released["status"] == "sent" and deliveries == [ADDRESS]


def test_conditions_require_authorized_contacts_and_signed_delivery_times(log, deliveries, monkeypatch):
    monkeypatch.setitem(rules.CONFIG, "release_conditions",
                        {"value": {"order": ["dock_slot_confirmed", "transport_confirmed"]}})
    monkeypatch.setitem(rules.CONFIG["staff_email"]["value"], "Katrin Jansen", "dock@example.test")
    action = order()
    delivery = release.terms_for(action)["delivery_id"]
    monkeypatch.setitem(rules.CONFIG, "release_carrier_contacts", {"value": {delivery: "carrier@example.test"}})

    def confirm(kind, body, address, delivery_id=delivery, *, summary=None):
        origin = actions.propose(FINDING, "Check arrangement", "", "confirm_terms", 0, 0, 0, None, None,
                                 payload={"delivery_id": delivery_id, "confirmation_kind": kind},
                                 messages=[message(address, body="Please confirm availability.")])
        outbound._mark(origin["messages"][0]["id"], "sent")
        reply_from(origin, handled=True, text=body)
        if summary:
            with actions.tx() as c:
                c.execute("UPDATE replies SET summary_en=? WHERE action_id=?", (summary, origin["id"]))
        return outbound.dispatch(action["id"])

    held = approve(action)
    assert "no linked reply from an authorized contact" in held["held_reason"]
    carrier = "carrier@example.test"
    dock = "dock@example.test"
    confirmed = "Truck is confirmed. Booking ref WT-123, ETA Sun 10:30."
    assert "not authorized" in confirm("book_truck", confirmed, "unknown@example.test")["held_reason"]
    assert "affirmative confirmation" in confirm("book_truck", "If the carrier confirmed booking WT-123, ETA Sun 10:30.",
                                                 carrier)["held_reason"]
    assert "booking reference missing, ETA missing" in confirm("book_truck", "Truck is confirmed.", carrier,
                                                               summary=confirmed)["held_reason"]
    assert "outside the signed delivery" in confirm("book_truck", "Carrier confirmed booking WT-123, ETA Mon 13:00.",
                                                    carrier)["held_reason"]
    assert "outside the signed delivery" in confirm("book_truck", "Carrier confirmed booking WT-123, ETA Mon 12:00:01.",
                                                    carrier)["held_reason"]
    assert "ETA missing" in confirm("book_truck", "Carrier confirmed booking WT-123, ETA Sun 10:30 PM.",
                                    carrier, summary=confirmed)["held_reason"]
    assert "dock slot confirmed" in confirm("book_truck", confirmed, carrier)["held_reason"]
    window = "Dock slot confirmed for Sun 07:00–12:00."
    assert "no linked reply" in confirm("dock_slot", window, dock, "unrelated-shipment")["held_reason"]
    assert "not authorized" in confirm("dock_slot", window, "unknown-dock@example.test")["held_reason"]
    assert "slot window missing" in confirm("dock_slot", "Dock slot is confirmed.", dock)["held_reason"]
    assert "does not cover" in confirm("dock_slot", "Dock slot confirmed for Mon 07:00–12:00.", dock)["held_reason"]
    held = confirm("dock_slot", "Dock slot confirmed for Sun 07:00–10:00.", dock)
    card = release.decision_state(held)
    assert card["hold_owner"] == "Receiving team"
    assert "Carrier confirmed booking WT-123, ETA Sun 10:30" in card["hold_clears"]
    assert card["confirmation_evidence"]["dock_slot_confirmed"]["slot_end"] == "2026-11-22T10:00:00+01:00"
    released = confirm("dock_slot", window, dock)
    assert released["status"] == "sent"
    assert released["payload"]["confirmation_evidence"]["dock_slot_confirmed"]["contact"] == "Katrin Jansen"
    assert deliveries == [ADDRESS]


def test_supplier_logistics_and_deputy_can_confirm_using_corroborated_summary(log, deliveries, monkeypatch):
    monkeypatch.setitem(rules.CONFIG, "release_supplier_logistics_contacts",
                        {"value": {SUPPLIER: "logistics@example.test"}})
    monkeypatch.setitem(rules.CONFIG["staff_email"]["value"], "Raj Patel", "deputy@example.test")
    action = order(payload={"conditions": ["transport_confirmed", "dock_slot_confirmed"]})
    delivery = release.terms_for(action)["delivery_id"]
    assert "required condition missing" in approve(action)["held_reason"]
    for kind, address, body, summary in (
        ("book_truck", "logistics@example.test", "Transport potwierdzony. Numer rezerwacji WT-123. Sun 10:30.",
         "Carrier confirmed booking WT-500; ETA Sun 10:30."),
        ("dock_slot", "deputy@example.test", "Slot 07:00–12:00 confirmed.", None),
    ):
        origin = actions.propose(FINDING, "Check arrangement", "", "confirm_terms", 0, 0, 0, None, None,
                                 payload={"delivery_id": delivery, "confirmation_kind": kind},
                                 messages=[message(address, body="Please confirm availability.")])
        outbound._mark(origin["messages"][0]["id"], "sent")
        reply_from(origin, handled=True, text=body)
        with actions.tx() as c:
            c.execute("UPDATE replies SET summary_en=? WHERE action_id=?", (summary, origin["id"]))
    released = outbound.dispatch(action["id"])
    assert released["status"] == "sent"
    evidence = released["payload"]["confirmation_evidence"]
    assert evidence["transport_confirmed"]["booking_reference"] == "WT-123"
    assert evidence["dock_slot_confirmed"]["contact"] == "Raj Patel"
    assert deliveries == [ADDRESS]


@pytest.mark.parametrize("source,issue", [
    ("suppliers/SUP-0028_Karton_Nowak/profile.json", "raw load failed"),
    ("material-update.csv", "invalid update for CMP-GAM-008"),
    ("supplier-confirmation.xlsx#Orders", f"invalid date for {PO}"),
    ("po-export.csv", "invalid spreadsheet"),
])
def test_related_ingest_issues_hold_until_rebuilt(log, deliveries, source, issue):
    action = order()
    with sqlite3.connect(rules.DB_PATH) as db:
        db.execute("UPDATE purchase_orders SET source='po-export.csv'")
        db.execute("INSERT INTO ingest_issues VALUES (?,?)", (source, issue))
        db.execute("INSERT INTO ingest_issues VALUES (?,?)", ("suppliers/SUP-0128/profile.json", "invalid JSON"))
    assert approve(action)["held_reason"] == f"evidence stale: ingest issue in {source}"
    assert deliveries == []
    with sqlite3.connect(rules.DB_PATH) as db:
        db.execute("DELETE FROM ingest_issues WHERE path=?", (source,))
    assert outbound.dispatch(action["id"])["status"] == "sent"
    assert deliveries == [ADDRESS]


def test_failed_rebuild_invalidates_release_checkpoint(log, deliveries, monkeypatch):
    action = order()
    monkeypatch.setenv("SOONY_TOKEN", "fixture")
    monkeypatch.setattr(server.soony_sync, "sync_once", lambda _: [])
    monkeypatch.setattr("core.extract_docs.documents", lambda: [])

    def fail_build():
        raise OSError("rebuild failed")

    monkeypatch.setattr(ingest, "build", fail_build)
    monkeypatch.setitem(server.STATE, "files_pending", True)
    assert approve(action)["held_reason"] == "evidence stale"
    assert server.STATE["files_pending"]
    assert deliveries == []


def test_historical_release_gate_replay():
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / "examples/replay_gate.py")]
    result = subprocess.run(command, cwd=root, check=True, capture_output=True, text=True)
    assert result.stdout == subprocess.check_output(command, cwd=root, text=True)
    documented, position_case = result.stdout.split("\n\n07:01 CARRIER POSITION", 1)
    assert documented.strip() in (root / "README.md").read_text()
    assert "gate and cockpit agree: verified, carrier_position" in position_case
    assert "Carrier position TRUCK-1 confirms departure by Sun 06:40." in position_case
    assert "RELEASED WITH SYNTHETIC EVIDENCE" in result.stdout
    assert "HELD — draft quantity disagrees with the commitment" in result.stdout
    assert "07:01 RETRY WITH CONFIRMED SLOT — HELD — departure deadline passed" in result.stdout
    assert "07:01 REVISED TERMS RELEASED — verified departure Sun 06:40, ETA Sun 14:30" in result.stdout


def test_new_document_disagrees_with_commitment(log, deliveries):
    action = order()
    with sqlite3.connect(rules.DB_PATH) as db:
        db.execute("INSERT INTO sources(path,last_changed) VALUES (?,?)", ("revised-offer.pdf", rules.now().isoformat()))
        db.execute("INSERT INTO doc_facts VALUES (?,?,?,?)", ("revised-offer.pdf", "offer",
                   json.dumps({"supplier_id": SUPPLIER, "material_id": PART, "qty": 4000, "unit_price_eur": 2.1}), "fixture"))
    assert "supplier offer disagrees on quantity" in approve(action)["held_reason"]
    assert deliveries == []


def test_cycle_polls_and_syncs_before_approved_release_retry(log, deliveries, monkeypatch):
    action = order(protected=300000)
    assert action["status"] == "proposed"
    actions.decide(action["id"], "approve", "Paul Winter", source="dashboard",
                   commitment_digest=release.approval_digest(action))
    monkeypatch.setenv("SOONY_NOW", "2026-11-22T10:16:00+01:00")
    monkeypatch.setenv("SOONY_TOKEN", "fixture")
    monkeypatch.setenv("IMAP_HOST", "fixture")
    monkeypatch.setitem(server.STATE, "files_pending", False)
    origin = actions.propose(FINDING, "Check terms", "", "ask", 0, 0, 0, None, None, messages=[message()])
    outbound._mark(origin["messages"][0]["id"], "sent")
    calls = []

    def sync(_):
        calls.append("files")
        return []

    def fetch():
        calls.append("mailbox")
        reply_from(origin, handled=False)
        return []

    monkeypatch.setattr(server.soony_sync, "sync_once", sync)
    monkeypatch.setattr(inbox, "fetch", fetch)
    monkeypatch.setattr(server.chase, "run", lambda: {"actions": []})
    monkeypatch.setattr(server.voice, "prepare_open", lambda: [])
    monkeypatch.setattr(server.voice, "collect", lambda: [])
    server.cycle()
    held = actions.get(action["id"])
    assert calls == ["files", "mailbox"]
    assert held["status"] == "approved" and "unread supplier reply" in held["held_reason"]
    assert deliveries == []


def test_discord_decisions_use_account_id(log, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from server import discord_bot

    monkeypatch.setitem(rules.CONFIG, "staff_discord_accounts", {"value": {"7": "Paul Winter", "8": "Katrin Jansen"}})
    action = order()
    bot = discord_bot.Bot(server.decide_and_dispatch)

    def interaction(user_id, display_name):
        return SimpleNamespace(
            type=discord_bot.discord.InteractionType.component,
            data={"custom_id": f"dock:reject:{action['id']}"},
            user=SimpleNamespace(id=user_id, display_name=display_name),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

    async def decide():
        unknown = interaction(6, "Paul Winter")
        await bot.on_interaction(unknown)
        assert "not assigned" in unknown.response.send_message.call_args.args[0]
        wrong = interaction(8, "Paul Winter")
        await bot.on_interaction(wrong)
        assert "requires a verified decision" in wrong.followup.send.call_args.args[0]
        assert actions.get(action["id"])["status"] == "proposed"
        await bot.on_interaction(interaction(7, "Different display name"))

    asyncio.run(decide())
    rejected = actions.get(action["id"])
    assert rejected["status"] == "rejected"
    assert "Paul Winter (Discord)" in rejected["history"][-1]["entry"]


@pytest.mark.parametrize("body", [
    "Please dispatch the 8,000 art books.",
    "Could you dispatch the 8,000 art books?",
    "Bitte versenden Sie die Bücher.",
    "Proszę o potwierdzenie terminu. Proszę wysłać 8 000 sztuk.",
    "아트북 8000권을 발송해 주세요.",
    "请发货8000册。",
    "8,000 art books at €2.10",
    "Please book a truck for pickup.",
])
def test_zero_cost_ask_cannot_send_an_instruction(log, deliveries, body):
    a = actions.propose(FINDING, "Ask supplier", "", "ask", 0, 0, 0, None, None,
                        messages=[message(body=body)])
    held = outbound.dispatch(a["id"])
    assert held["held_reason"] == "typed commitment is missing"
    assert deliveries == []


def test_zero_cost_delivery_status_question_still_sends(log, deliveries):
    a = actions.propose(FINDING, "Ask supplier", "", "ask", 0, 0, 0, None, None,
                        messages=[message(body="Please confirm when the 8,000 art books shipped.")])
    assert outbound.dispatch(a["id"])["messages"][0]["status"] == "sent"
    assert deliveries == [ADDRESS]


@pytest.mark.parametrize(("body", "reason"), [
    ("Please dispatch the 4,000 art books.", "draft quantity"),
    ("8,000 art books at PLN 2.10", "draft price or currency"),
    ("8,000 art books at EUR 2.85", "draft price or currency"),
])
def test_draft_terms_that_disagree_are_held(log, deliveries, body, reason):
    action = order(body=body)
    assert reason in action["held_reason"]
    with pytest.raises(ValueError, match=reason):
        approve(action)
    assert not release.decision_state(action)["can_approve"]
    assert deliveries == []


def test_signed_terms_are_the_same_in_approval_and_supplier_email_and_repeat(client, deliveries, monkeypatch):
    from server import discord_bot

    monkeypatch.setenv("PUBLIC_URL", "https://dockside.example.test")
    monkeypatch.setattr(server.dispatch, "snapshot", lambda _: {"decisions": [{"id": action["id"]}]})
    action = order(body="Please dispatch 8,000 art books at EUR 2.10. Unreviewed note about delivery.")
    terms = release.terms_for(action)
    block = release.terms_block(terms)
    approvals.email(action["id"])
    approval, = deliveries.messages
    assert block in approval.get_content()
    decision, = client.get("/api/dispatch").json()["decisions"]
    assert block in decision["instruction"]
    discord = discord_bot.decision_embed(action)
    assert block in "".join(field.value for field in discord.fields if field.name.startswith("Shipment instruction"))

    old_card = client.post(f"/api/actions/{action['id']}/decide", json={"decision": "approve"},
                           headers={"Tailscale-User-Login": "paul@example.test"})
    assert old_card.status_code == 409
    assert deliveries == ["paul@example.test"]
    original = approve(action)
    shipment_mail = deliveries.messages[-1]
    assert block in shipment_mail.get_content()
    assert "Unreviewed note" not in shipment_mail.get_content()
    assert original["signatures"]["Paul Winter"]["commitment_digest"] == shipment_mail["X-Dockside-Terms-SHA256"]

    repeat = actions.propose("followup-original", "Repeat shipment instruction", "", "ask", 0, 0, 0, None, None,
                             parent=original, repeat_commitment=True, messages=[message(body="Please dispatch 8,000 art books.")])
    assert outbound.dispatch(repeat["id"])["messages"][0]["status"] == "sent"
    repeat_mail = deliveries.messages[-1]
    assert repeat_mail.get_content().split("\n\n", 1)[1] == shipment_mail.get_content().split("\n\n", 1)[1]
    assert repeat_mail["X-Dockside-Commitment"] == shipment_mail["X-Dockside-Commitment"]
    assert repeat_mail["X-Dockside-Terms-SHA256"] == shipment_mail["X-Dockside-Terms-SHA256"]
    assert "Do not create an additional order or booking" in repeat_mail.get_content()


def test_repeat_cannot_reuse_an_unsigned_instruction(log, deliveries):
    repeat = actions.propose("followup", "Repeat", "", "ask", 0, 0, 0, None, None,
                             parent=order(), repeat_commitment=True, messages=[message(body="Dispatch 8,000 art books.")])
    assert "explicitly approved and sent" in outbound.dispatch(repeat["id"])["held_reason"]
    assert deliveries == []


def test_outbox_retry_rejects_change_to_signed_delivery_place(log, deliveries, monkeypatch):
    monkeypatch.delenv("SMTP_HOST")
    action = approve(order())
    assert action["messages"][0]["status"] == "outbox"
    payload = action["payload"]
    terms = payload["commitment"]
    terms["delivery_place"] = "Rotterdam"
    payload["commitment_digest"] = release.fingerprint(terms)
    with actions.tx() as db:
        db.execute("UPDATE actions SET payload=? WHERE id=?", (json.dumps(payload), action["id"]))
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    assert "approval for these exact terms is missing" in outbound.dispatch(action["id"])["held_reason"]
    assert deliveries == []


@pytest.mark.parametrize("source", ["files", "mailbox"])
def test_failed_preflight_invalidates_still_fresh_checkpoint(log, deliveries, monkeypatch, source):
    action = order()
    monkeypatch.setenv("SOONY_TOKEN", "fixture")
    monkeypatch.setenv("IMAP_HOST", "fixture")
    monkeypatch.setitem(server.STATE, "files_pending", False)

    def fail(*_):
        raise OSError("preflight failed")

    monkeypatch.setattr(server.soony_sync, "sync_once", fail if source == "files" else lambda _: [])
    monkeypatch.setattr(inbox, "fetch", fail if source == "mailbox" else lambda: [])
    held = approve(action)
    assert held["held_reason"] == "evidence stale"
    assert release.decision_state(held)["hold_owner"] == "Dockside operator"
    with actions.tx() as c:
        assert c.execute("SELECT checked_at FROM release_evidence WHERE source=?", (source,)).fetchone()[0] is None
    assert deliveries == []
    release.record_check(source)
    assert outbound.dispatch(action["id"])["status"] == "sent"


def test_terminal_decision_cards_hide_approve(client, monkeypatch):
    expired = order(payload={"latestUsefulAt": "2026-11-22T09:59:00+01:00"})
    replaced = order()
    actions.hold(replaced["id"], f"duplicate of {expired['id']}; already sent", supersede=True)
    monkeypatch.setattr(server.dispatch, "snapshot", lambda _: {"decisions": [{"id": expired["id"]}]})
    cards = client.get("/api/dispatch").json()["decisions"]
    assert all(not card["can_approve"] for card in cards)
    assert [card["terminal"] for card in cards] == ["Expired — no longer useful", f"Replaced by {expired['id']}"]
    node = shutil.which("node")
    if node:
        js = Path(__file__).resolve().parents[1] / "web/assembly/operations.js"
        render = 'const html = value => value; const money = String; const at = String;\n' + js.read_text()
        render += "\nconsole.log(" + json.dumps(cards) + ".map(decisionItem).join(''));"
        result = subprocess.run([node, "-"], input=render, text=True, check=True, capture_output=True)
        assert 'data-decision="approve"' not in result.stdout
        assert all(card["terminal"] in result.stdout for card in cards)


def test_historical_zero_cost_resend_is_held_on_outbox_retry(log, deliveries):
    original = actions.propose(FINDING, "Follow up", "", "ask", 0, 0, 0, None, None,
                               messages=[message(body="Please confirm the arrival date.")])
    with actions.tx() as db:
        db.execute("UPDATE messages SET status='outbox',body=? WHERE action_id=?",
                   ("Please dispatch the 8,000 art books.", original["id"]))
    assert not original["held_reason"]
    held = outbound.dispatch(original["id"])
    assert held["held_reason"] == "typed commitment is missing"
    assert held["messages"][0]["status"] == "outbox"
    assert deliveries == []


def test_freight_instruction_waits_for_approval_even_with_high_value_ratio(log, deliveries):
    carrier = "carrier@example.test"
    with sqlite3.connect(rules.DB_PATH) as db:
        db.execute("INSERT INTO suppliers(supplier_id,status,contact_email) VALUES ('FR-01','active',?)", (carrier,))
    booking = actions.propose("freight-booking", "Book art book pickup", "", "book_truck", 168, 50000, 0, "Print & Packaging", None,
                              payload={"commitment": {"part": PART, "quantity": 8000, "unit_price": 0.021, "currency": "EUR",
                                                      "incoterm": "DAP", "delivery_place": "Hamburg", "deliver_by": "2026-11-23",
                                                      "po": PO, "po_line": 10, "valid_until": "2026-11-22"}},
                              messages=[message(carrier, body="Please book freight for 8,000 art books. Total price EUR 168.")])
    assert booking["status"] == "proposed"
    assert outbound.dispatch(booking["id"])["status"] == "proposed"
    assert deliveries == []
    assert approve(booking)["status"] == "sent"
    assert deliveries == [carrier]
    assert "Book freight according to these terms." in deliveries.messages[0].get_content()


def test_email_link_approves_the_exact_terms_in_its_message(client, deliveries, monkeypatch):
    import re

    monkeypatch.setenv("PUBLIC_URL", "https://dockside.example.test")
    action = order()
    approvals.email(action["id"])
    path = re.search(r"https://dockside.example.test(/approval/[\w-]+/approve)", deliveries.messages[0].get_content())[1]
    preview = client.get(path)
    assert preview.status_code == 200 and action["commitment_id"] in preview.text
    result = client.post(path)
    assert "was approved" in result.text
    assert deliveries == ["paul@example.test", ADDRESS]
    stored = actions.get(action["id"])
    assert stored["signatures"]["Paul Winter"]["commitment_digest"] == release.approval_digest(action)


def test_staff_answer_on_dashboard_joins_the_reply_queue(log, monkeypatch):
    from core import inbox, release
    from server import app as server
    a = actions.propose("gap-CMP-GAM-008", "Give the PO number", "supplier needs it", "ask", 0, 0, 0, None, None,
                        messages=[{"channel": "discord", "to_name": "Paul Winter", "to_address": None, "language": "en",
                                   "subject": "PO number", "body": "Please name the PO by 02:25."}])
    mid = a["messages"][0]["id"]
    monkeypatch.setattr(server, "verified_person", lambda request: ("Paul Winter", "dashboard"))
    client = TestClient(server.app)
    ok = client.post(f"/api/questions/{mid}/answer", json={"text": "Use 4500318775"})
    assert ok.status_code == 200
    reply = inbox.get_reply(ok.json()["reply_id"])
    assert reply["from_name"] == "Paul Winter" and reply["msg_id"] == mid and reply["handled"] == 0
    monkeypatch.setattr(server, "verified_person", lambda request: None)
    assert client.post(f"/api/questions/{mid}/answer", json={"text": "x"}).status_code == 403


def test_answer_is_refused_from_someone_else(log, monkeypatch):
    from server import app as server
    a = actions.propose("gap-CMP-GAM-008", "Give the PO number", "supplier needs it", "ask", 0, 0, 0, None, None,
                        messages=[{"channel": "discord", "to_name": "Paul Winter", "to_address": None, "language": "en",
                                   "subject": "PO number", "body": "Please name the PO."}])
    mid = a["messages"][0]["id"]
    monkeypatch.setattr(server, "verified_person", lambda request: ("Katrin Jansen", "dashboard"))
    refused = TestClient(server.app).post(f"/api/questions/{mid}/answer", json={"text": "Use 4500318775"})
    assert refused.status_code == 403 and "Paul Winter" in refused.json()["detail"]
