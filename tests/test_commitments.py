import datetime as dt
import json
import sqlite3

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
        db.execute("""INSERT INTO purchase_orders(po_number,supplier_id,material_id,quantity,unit_price,currency,status)
                      VALUES (?,?,?,8000,2.1,'EUR','open')""", (PO, SUPPLIER, PART))
    release.record_check("files")
    release.record_check("mailbox")


@pytest.fixture
def client(log):
    return TestClient(server.app, client=("127.0.0.1", 12345))


@pytest.fixture
def deliveries(log, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("MAIL_FROM", "dockside@example.test")
    sent = []

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

    monkeypatch.setattr(outbound.smtplib, "SMTP", SMTP)
    return sent


def message(to=ADDRESS, *, po=PO):
    return {"channel": "email", "to_name": "Supplier", "to_address": to, "language": "en",
            "subject": "Order", "body": "8,000 units", "po_numbers": [po] if po else []}


def order(finding=FINDING, *, to=ADDRESS, protected=1000, payload=None, parent=None):
    terms = {"quantity": 8000, "unit_price_eur": 2.1, "valid_until": "2026-11-22", **(payload or {})}
    return actions.propose(finding, "Order 8,000 art books", "rush", "order", 16800, protected, 2500,
                           "Print & Packaging", None, payload=terms, messages=[message(to)], parent=parent)


def approve(action, source="dashboard", person="Paul Winter"):
    return server.decide_and_dispatch(action["id"], "approve", person, source=source)


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
    result = client.post(f"/api/actions/{action['id']}/decide", json={"decision": "approve"},
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
    assert "expired" in approve(expired)["held_reason"]
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


def test_condition_requires_correlated_supplier_confirmation(log, deliveries, monkeypatch):
    monkeypatch.setitem(rules.CONFIG, "release_conditions",
                        {"value": {"order": ["dock_slot_confirmed", "transport_confirmed"]}})
    action = order()
    held = approve(action)
    assert "dock slot confirmed" in held["held_reason"]
    for kind, body, address in (
        ("dock_slot", "Dock slot is confirmed.", "dock@example.test"),
        ("book_truck", "Truck is confirmed.", "carrier@example.test"),
    ):
        origin = actions.propose(FINDING, "Confirm arrangement", "", kind, 0, 0, 0, None, None, messages=[message(address)])
        outbound._mark(origin["messages"][0]["id"], "sent")
        reply_from(origin, handled=True, text=body)
    assert outbound.dispatch(action["id"])["status"] == "sent"
    assert deliveries == [ADDRESS]


def test_new_document_disagrees_with_commitment(log, deliveries):
    action = order()
    with sqlite3.connect(rules.DB_PATH) as db:
        db.execute("INSERT INTO sources(path,last_changed) VALUES (?,?)", ("revised-offer.pdf", rules.now().isoformat()))
        db.execute("INSERT INTO doc_facts VALUES (?,?,?,?)", ("revised-offer.pdf", "offer",
                   json.dumps({"supplier_id": SUPPLIER, "material_id": PART, "qty": 4000, "unit_price_eur": 2.1}), "fixture"))
    assert "supplier offer disagrees on quantity" in approve(action)["held_reason"]
    assert deliveries == []


def test_cycle_polls_and_syncs_before_due_paid_release(log, deliveries, monkeypatch):
    action = order(protected=300000)
    assert action["status"] == "scheduled"
    monkeypatch.setenv("PUBLIC_URL", "https://dockside.example.test")
    approvals.email(action["id"])
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
    assert held["status"] == "scheduled" and "unread supplier reply" in held["held_reason"]
    assert deliveries == ["paul@example.test"]


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
