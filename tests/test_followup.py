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
    policy["release_carrier_feeds"] = {"value": {replay.ORDER: "WARTA"}}
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
        # The next update carries the same booking but doesn't report a new departure time.
        replay.store_reply(carrier, "refreshed-eta", "Carrier confirmed booking WT-123, ETA Sun 14:30. Gross vehicle weight is 7.5 t.")
        decision = release.decision_state(actions.get(late["id"]))
        assert decision["can_approve"]
        assert decision["departure"]["evidence_id"] == "verified"

        feed = replay.thread("Warta Trans", replay.CARRIER_EMAIL, "Please send the latest ETA.", "book_truck", replay.ORDER)
        replay.store_reply(feed, "feed-eta", "Carrier confirmed booking WT-123, ETA Sun 14:30. Gross vehicle weight is 7.5 t.")
        with sqlite3.connect(rules.DB_PATH) as db:
            db.execute("INSERT INTO carriers(carrier_id, name) VALUES ('WARTA', 'Warta Trans')")
            db.execute("INSERT INTO sites(site_id, name, lat, lon) VALUES ('PICKUP', 'Karton Nowak', 52.4, 16.8)")
            db.execute("""INSERT INTO shipments(shipment_id,carrier_id,mode,reference,supplier_id,material_ids,
                                                origin,dest_site,status,last_lat,last_lon,updated_at)
                          VALUES ('TRUCK-1','WARTA','road','WT-123',?,?,'PICKUP','DC-HAM','in_transit',52.40001,16.8,?)""",
                       (replay.SUPPLIER, replay.PART, replay.at("07:01").isoformat()))
        with pytest.raises(ValueError, match="departure deadline passed"):
            replay.sign(late)
        with sqlite3.connect(rules.DB_PATH) as db:
            db.execute("UPDATE shipments SET updated_at=?", (replay.at("06:40").isoformat(),))
        with pytest.raises(ValueError, match="position too close to origin"):
            replay.sign(late)
        decision = release.decision_state(actions.get(late["id"]))
        assert decision["held_reason"] == "position too close to origin"
        assert not decision["can_approve"]
        with sqlite3.connect(rules.DB_PATH) as db:
            db.execute("UPDATE shipments SET last_lat=52.5")
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


def test_disputed_rush_order_updates_trace_forecast_and_followup(tmp_path, monkeypatch):
    from contextlib import closing

    from fastapi.testclient import TestClient

    from core import depgraph, dispatch, questions
    from server import app as server
    from server.discord_bot import open_questions

    replay = replay_gate
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    monkeypatch.setattr(rules, "DB_PATH", tmp_path / "master.db")
    policy = customer.load(replay.ROOT / "config" / "business.json")
    policy["release_carrier_contacts"] = {"value": {replay.ORDER: replay.CARRIER_EMAIL}}
    policy["followup"]["value"].update(enabled=True)
    policy["release_supplier_logistics_contacts"] = {"value": {}}

    def ask_buyer(finding, recipient="Paul Winter", *, kind="ask", delivery_id=None):
        draft = {"channel": "discord", "to_name": recipient, "to_address": None, "language": "en",
                 "subject": "Name the purchase order", "body": "Please give the order reference."}
        action = actions.propose(finding, "Name the purchase order", "", kind, 0, 0, 0, None, None,
                                 messages=[draft], payload={"delivery_id": delivery_id})
        mid = action["messages"][0]["id"]
        with actions.tx() as log:
            log.execute("UPDATE messages SET status='delivered', sent_at=? WHERE id=?", (rules.now().isoformat(), mid))
        return mid, draft

    def recovery():
        with closing(actions.con()) as log:
            related = dispatch.linked_actions(log)[replay.FINDING]
        with closing(sqlite3.connect(rules.DB_PATH)) as db:
            offer, = rules.facts(db, "offer")
            edition = "FG-GH6-CE"
            return dispatch.recovery(
                db, offer, {replay.SUPPLIER: "Karton Nowak"}, related,
                {edition: {"projected": 2500, "constraintIds": [], "capacityLimited": False}},
                {edition: {"projected": 0}}, replay.at("16:00"), rules.now(),
            )

    with rules.replay(policy, replay.at("00:14")):
        replay.seed_sent_order()
        with closing(sqlite3.connect(rules.DB_PATH)) as db, db:
            db.execute("INSERT INTO launch_status VALUES ('FG-GH6-CE', ?, 8000, 0, 8000, 900, 'test')",
                       (rules.now().isoformat(),))
            db.execute("INSERT INTO bom(parent_id, child_id, qty_per) VALUES ('FG-GH6-CE', ?, 1)", (replay.PART,))
        original = actions.get(replay.ORDER)
        supplier = release.sent_order_message(original)
        carrier = replay.thread("Warta Trans", replay.CARRIER_EMAIL, "Please confirm transport.", "book_truck", replay.ORDER)
    with rules.replay(policy, replay.at("00:18")):
        replay.store_reply(supplier, "ack", "We acknowledge receipt of the order.")
        older, _ = ask_buyer("reply-ack")
        pre_dispute, _ = ask_buyer(f"followup-{replay.FINDING}-202611220018", "Head of Procurement", kind="escalate")
        unrelated, _ = ask_buyer(replay.FINDING, "Jonas Lindqvist", delivery_id="another-order")
        assert questions.find(older)["kind"] == "message"
    with rules.replay(policy, replay.at("02:07")):
        replay.store_reply(supplier, "accept", replay.ACCEPTANCE)
        route, option = recovery()
        formal = next(row for row in route["steps"] if row["name"] == "Formal order")
        assert formal["state"] == "confirmed"
        assert option["deliveryState"]["conflict"] is None

    denial = "We received no dispatch instruction. We can load within four hours."
    with rules.replay(policy, replay.at("04:17")):
        replay.store_reply(supplier, "denial", denial,
                           "Karton Nowak reports no dispatch instruction; using the 04:17 reply the earliest modeled "
                           "unload is 17:17, so ACT-B7B90B recorded the delivery as unconfirmed.")
        client = TestClient(server.app, raise_server_exceptions=True)
        before_get = actions.DB.read_bytes()
        response = client.get("/api/dispatch")
        assert response.status_code == 200
        assert response.json()["components"][0]["option"]["deliveryState"]["conflict"]
        assert actions.DB.read_bytes() == before_get
        questions.reconcile()
        route, option = recovery()
        formal = next(row for row in route["steps"] if row["name"] == "Formal order")
        assert formal["state"] == "waiting"
        assert formal["detail"] == "Disputed by Karton Nowak at 04:17."
        assert [(row["message_id"], row["kind"]) for row in formal["events"][1:]] == [
            ("ack", "acknowledged"), ("accept", "accepted"), ("denial", "disputed"),
            ("denial", "estimate"), ("denial", "note"),
        ]
        dispute, estimate, note = formal["events"][-3:]
        assert estimate["who"] == "Dockside" and "17:17" in estimate["text"]
        assert dispute["text"] == "Karton Nowak reports no dispatch instruction"
        assert note["who"] == "Dockside" and "ACT-B7B90B" in note["text"]
        assert server.get_reply(dispute["message_id"])["body"] == denial
        monkeypatch.setenv("OPENAI_API_KEY", "fixture")
        monkeypatch.setattr(agent, "translate_reply", lambda body: pytest.fail("English replies should bypass translation"))
        source = client.get(f"/api/replies/{dispute['message_id']}/view")
        assert source.status_code == 200
        assert denial in source.text and source.text.count("<section>") == 1
        monkeypatch.delenv("OPENAI_API_KEY")
        assert source.text == client.get(f"/api/replies/{dispute['message_id']}/view").text

        delivery = option["deliveryState"]
        assert delivery["conflict"]["owner_reconcile"] == "Paul Winter"
        assert delivery["conflict"]["owner_logistics"] == "Raj Patel"
        assert delivery["can_propose_revision"] is False
        assert option["timingState"] == "disputed"
        assert option["modeledUnloadAt"] == replay.at("17:17").isoformat()
        assert option["modeledUnloadAnchorAt"] == replay.at("04:17").isoformat()
        assert option["modeledUnloadReplyId"] == "denial" and option["modeledLateMinutes"] == 77
        assert option["modeledUnloadBasis"] == "If loading takes 4 h from the dispute reply at 04:17 plus 9 h drive"
        task_id = option["task"]["id"]
        assert task_id == f"task:{replay.ORDER}:denial"
        assert route["next_step"]["task"] == option["task"]
        assert client.get("/api/dispatch").json()["components"][0]["option"]["task"]["id"] == task_id
        assert questions.find(older)["reminder_of"] == task_id
        assert "17:17 misses the 16:00" in route["next_step"]["text"]
        assert route["next_step"]["reply_id"] == "denial"
        terms = release.delivery_terms(original)
        amendment = agent.Tools().propose_action(
            f"followup-{replay.FINDING}-202611220417", "Amend the art book delivery deadline", "", "", "order",
            16800, 708000, 5900, "Print & Packaging", "Dockside", 15,
            [replay.email("Karton Nowak", replay.SUPPLIER_EMAIL, "Change the arrival time on the existing order.")],
            commitment={**terms, "deliver_by": replay.at("15:00").isoformat(), "revises_action_id": replay.ORDER},
            delivery_id=replay.ORDER,
        )
        assert "disputes the order" in amendment["error"]

    with rules.replay(policy, replay.at("04:19")):
        newer, draft = ask_buyer(f"followup-{replay.FINDING}-202611220419")
        escalation, _ = ask_buyer(f"followup-{replay.FINDING}-202611220419", "Jonas Lindqvist",
                                  kind="escalate", delivery_id=replay.ORDER)
        reminder_action = agent.Tools().propose_action(
            "reply-denial", "Ask again", "", "", "escalate", 0, 0, 0, None, "Dockside", 15,
            [draft | {"to_name": "Dr. Maren Vogt"}],
        )["done_automatically"]
        reminder = actions.get(reminder_action)["messages"][0]
        assert task_id in reminder["body"]

    with rules.replay(policy, replay.at("04:25")):
        replay.store_reply(supplier, "denial2", "We still have no dispatch instruction.",
                           "Karton Nowak reports no dispatch instruction. Dockside escalated the conflict.")
    with rules.replay(policy, replay.at("04:47")):
        replay.store_reply(supplier, "checking", "We are still checking.")
        questions.reconcile()
        route, option = recovery()
        assert option["task"]["id"] == task_id
        assert option["modeledUnloadReplyId"] == "denial2"
        assert option["modeledUnloadAnchorAt"] == replay.at("04:25").isoformat()
        assert option["modeledUnloadAt"] == replay.at("17:25").isoformat()
        assert option["modeledLateMinutes"] == 85 and "04:25" in option["modeledUnloadBasis"]
        assert list(open_questions("Paul Winter")) == [task_id]
        assert list(open_questions("Dr. Maren Vogt")) == [task_id]
        assert set(open_questions("Jonas Lindqvist")) == {task_id, unrelated}
        assert not open_questions("Katrin Jansen")
        assert {q["id"] for q in agent.Tools().get_open_questions()} == {task_id, unrelated}
        assert questions.find(task_id)["source_reply_id"] == "denial2"
        replaced = {older, pre_dispute, newer, escalation, reminder["id"]}
        assert {r["superseded_message_id"] for r in route["steps"] if r.get("reminder_of") == task_id} == replaced
        assert all(questions.find(mid)["reminder_of"] == task_id for mid in replaced)
        part = {"id": replay.PART, "name": "Art books", "status": "short", "usable": 0, "required": 8000,
                "routes": [route], "default_route_id": route["id"], "next_step": route["next_step"], "option": option}
        item, = followup.stalled({"components": [part], "cutoff": replay.at("16:00").isoformat()})
        assert item["task"]["id"] == task_id
        assert [q["id"] for q in item["open_staff_questions"]] == [task_id]
        assert item["verified_eta"] is None
        assert all("See task" not in s for s in item["open_steps"])
        graph = depgraph.annotate({"nodes": [], "edges": [], "short": True}, part)
        rush = next(node for node in graph["nodes"] if node["id"] == route["id"])
        assert "Paul Winter's decision" in rush["detail"]
        assert "escalation to" not in rush["detail"] and "reminder to" not in rush["detail"]

        monkeypatch.setattr(server, "verified_person", lambda request: ("Katrin Jansen", "dashboard"))
        assert client.post(f"/api/questions/{task_id}/answer", json={"text": "Use the old PO"}).status_code == 403
        monkeypatch.setattr(server, "verified_person", lambda request: ("Dr. Maren Vogt", "dashboard"))
        # Context variables propagate from TestClient to the sync endpoint.
        for mid in replaced:
            refused = client.post(f"/api/questions/{mid}/answer", json={"text": "Use the old PO"})
            assert refused.status_code == 409 and task_id in refused.json()["detail"]
        recorded = client.post(f"/api/questions/{task_id}/answer", json={"text": questions.ORDER_STANDS})
        assert recorded.status_code == 200 and recorded.json()["task_id"] == task_id
        assert client.post(f"/api/questions/{task_id}/answer", json={"text": "duplicate"}).status_code == 409
        reply = next(r for r in agent.Tools().get_replies() if r["reply_id"] == recorded.json()["reply_id"])
        assert reply["staff_task"]["id"] == task_id
        assert reply["staff_task"]["answered_by"] == "Dr. Maren Vogt"
        assert not open_questions("Paul Winter")
        route, option = recovery()
        assert option["deliveryState"]["conflict"] and not option["deliveryState"]["transport_confirmed"]
        assert option["task"]["status"] == "answered"
        assert option["task"]["answer"] == questions.ORDER_STANDS
        assert option["task"]["answered_at"] == replay.at("04:47").isoformat()
        assert route["next_step"]["state"] == "Waiting for reply"
        assert "Answered by Dr. Maren Vogt" in route["next_step"]["text"]
        assert not any(row.get("message_id") == task_id for row in route["steps"])

        monkeypatch.delenv("SMTP_HOST", raising=False)
        inquiry = agent.Tools(handling_replies=True).propose_action(
            f"reply-{reply['reply_id']}", "Verify the art-book van", "", "", "confirm_terms",
            0, 0, 0, None, "Dockside", 15,
            [replay.email("Warta Trans", replay.CARRIER_EMAIL, "Please confirm the van booking and arrival time.")],
            delivery_id=replay.ORDER, confirmation_kind="book_truck",
        )
        request = actions.get(inquiry["done_automatically"])
        assert request["messages"][0]["status"] == "outbox"
        assert request["finding_id"].startswith(f"followup-{replay.FINDING}-")

    with rules.replay(policy, replay.at("05:00")):
        replay.store_reply(supplier, "reconciled", replay.ACCEPTANCE)
        assert release.delivery_state(original)["conflict"]
        replay.store_reply(carrier, "booking", "Carrier confirmed booking WT-123, ETA Sun 14:30. Gross vehicle weight is 7.5 t.")
        delivery = release.delivery_state(original)
        assert delivery["conflict"] is None
        assert release.verified_eta(delivery) == replay.at("14:30").isoformat()
        assert questions.find(task_id)["status"] == "closed"
        assert all(questions.find(mid)["superseded"] for mid in replaced)


def test_reply_source_shows_available_translation_for_non_english(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from server import app as server

    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    actions.initialize()
    body = "Nie otrzymaliśmy instrukcji wysyłki. Załadunek może rozpocząć się w ciągu czterech godzin."
    with actions.tx() as log:
        log.execute("INSERT INTO replies(id, from_addr, body, summary_en) VALUES (?,?,?,?)",
                    ("polish", "supplier@example.test", body, "The supplier reports no dispatch instruction."))
        log.execute("INSERT INTO replies(id, from_addr, body) VALUES (?,?,?)",
                    ("short", "supplier@example.test", "OK"))
    client = TestClient(server.app)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    summary = client.get("/api/replies/polish/view")
    assert body in summary.text and "English summary" in summary.text
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    monkeypatch.setattr(agent, "translate_reply", lambda text: "No dispatch instruction for <supplier>." if text == body else text)
    translated = client.get("/api/replies/polish/view")
    assert "English translation" in translated.text and "&lt;supplier&gt;" in translated.text
    assert client.get("/api/replies/short/view").text.count("<section>") == 1
    monkeypatch.delenv("OPENAI_API_KEY")
    assert client.get("/api/replies/polish/view").text == translated.text


def test_recorded_cancellation_queues_no_fee_request(tmp_path, monkeypatch):
    from core import inbox, outbound, questions

    replay = replay_gate
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    monkeypatch.setattr(rules, "DB_PATH", tmp_path / "master.db")
    policy = customer.load(replay.ROOT / "config" / "business.json")
    with rules.replay(policy, replay.at("00:14")):
        replay.seed_sent_order()
        original = actions.get(replay.ORDER)
        supplier = release.sent_order_message(original)
    with rules.replay(policy, replay.at("04:17")):
        replay.store_reply(supplier, "denial", "We received no dispatch instruction.")
        task, = questions.current()
        reply = questions.answer(task, "Paul Winter", questions.CANCEL_ORDER, channel="Discord")
        assert inbox.get_reply(reply["id"])["handled"] == 0
        assert not any(a["kind"] == "cancel_order" for a in actions.list_actions())

        inbox.mark_handled("denial", "Supplier reports no dispatch instruction.")
        summary = agent.handle_replies()
        action, = [a for a in actions.list_actions() if a["kind"] == "cancel_order"]
        assert action["id"] in summary and action["status"] == "auto"
        assert agent._propose_recorded_cancellation(reply["id"]) == action["id"]
        assert action["messages"][0]["status"] == "draft"
        assert not release.decision_state(action)["can_approve"]
        assert release.check(action) == (True, None)
        assert "authorizes no fee" in action["messages"][0]["body"]
        assert action["messages"][0]["to_address"] == supplier["to_address"]
        with actions.tx() as db:
            db.execute("UPDATE staff_tasks SET status='closed' WHERE id=?", (task["id"],))
        assert outbound.dispatch(action["id"])["held_reason"] == "the original order or recorded cancellation outcome changed"


@pytest.mark.parametrize("choice", ["keep_monday", "cancel"])
def test_post_deadline_separates_spending_approval_from_no_fee_cancellation(tmp_path, monkeypatch, choice):
    from core import questions

    replay = replay_gate
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    monkeypatch.setattr(rules, "DB_PATH", tmp_path / "master.db")
    policy = customer.load(replay.ROOT / "config" / "business.json")
    policy["release_carrier_contacts"]["value"] = {replay.ORDER: replay.CARRIER_EMAIL}
    policy["staff_email"]["value"]["Katrin Jansen"] = replay.DOCK_EMAIL

    def record_reminder(task):
        message = {"channel": "discord", "to_name": task["person"], "to_address": None, "language": "en",
                   "subject": f"Reminder of task {task['id']}", "body": task["text"]}
        notice = actions.propose(replay.FINDING, "Remind the buyer", "", "ask", 0, 0, 0, None, None, messages=[message])
        with actions.tx() as log:
            log.execute("UPDATE messages SET status='delivered', sent_at=? WHERE id=?",
                        (rules.now().isoformat(), notice["messages"][0]["id"]))

    with rules.replay(policy, replay.at("00:14")):
        replay.seed_sent_order()
        original = actions.get(replay.ORDER)
        supplier = release.sent_order_message(original)
    with rules.replay(policy, replay.at("06:59")):
        replay.store_reply(supplier, "denial", "We received no dispatch instruction.")
        earlier, = questions.current()
        assert not earlier["task_kind"]
        record_reminder(earlier)
    with rules.replay(policy, replay.at("07:01")):
        task, = questions.current()
        assert task["id"] != earlier["id"]
        assert task["person"] == "Paul Winter"
        assert task["person_role"] == "Buyer:Print & Packaging"
        assert task["task_kind"] == "post_deadline"
        assert task["reminded_at"] is None
        assert "07:00" in task["text"]
        assert questions.find(earlier["id"])["status"] == "closed"
        assert questions.order_state(original, now=rules.now())["task"]["id"] == task["id"]
        record_reminder(task)
        current, = questions.current()
        assert current["reminded_at"] == rules.now().isoformat()
        with pytest.raises(PermissionError):
            questions.answer(task, "Raj Patel", choice)
        reply = questions.answer(task, "Paul Winter", choice)
        with pytest.raises(ValueError, match="no longer open"):
            questions.answer(task, "Paul Winter", choice)
        proposal = actions.get(reply["proposal_id"])
        assert proposal["messages"][0]["status"] == "draft"
        if choice == "cancel":
            assert proposal["kind"] == "cancel_order"
            assert proposal["status"] == "auto"
            assert not reply["approval_requested"]
            assert release.check(proposal) == (True, None)
        else:
            assert proposal["status"] == "proposed"
            assert reply["approval_requested"]
            assert not release.check(proposal)[0]
            signed = replay.sign(proposal)
            terms = release.terms_for(signed)
            assert terms["deliver_by"] == "2026-11-23T16:00:00+01:00"
            assert terms["depart_by"] == "2026-11-23T07:00:00+01:00"
            assert terms["revises_action_id"] == replay.ORDER
            assert release.check(signed) == (False, "The buyer needs written supplier confirmation that the original order stands.")
    if choice == "keep_monday":
        with rules.replay(policy, replay.at("08:00")):
            replay.store_reply(supplier, "reconciled", replay.ACCEPTANCE)
            carrier = replay.thread("Warta Trans", replay.CARRIER_EMAIL, "Please confirm Monday's transport.", "book_truck", replay.ORDER)
            dock = replay.thread("Katrin Jansen", replay.DOCK_EMAIL, "Please confirm Monday's unload slot.", "dock_slot", replay.ORDER)
            replay.store_reply(carrier, "monday-transport", "Carrier confirmed booking WT-123, ETA Mon 14:30. Gross vehicle weight is 7.5 t.")
            replay.store_reply(dock, "monday-slot", "Dock slot confirmed for Mon 14:00–15:00.")
            for source in ("files", "mailbox"):
                release.record_check(source)
            assert release.check(signed) == (True, None)
