"""Replay the ACT-F74B32 terms and supplier acceptance in isolated SQLite logs."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import actions, customer, inbox, ingest, outbound, release, rules

ORDER = "ACT-F74B32"
DELIVERY = "ART-BOOKS-20261122"
SUPPLIER = "SUP-0028"
PART = "CMP-GAM-008"
SUPPLIER_EMAIL = "orders@karton-nowak.example"
CARRIER_EMAIL = "bookings@warta-trans.example"
DOCK_EMAIL = "katrin.jansen@soony.example"
OFFER_PATH = "suppliers/SUP-0028_Karton_Nowak/notices/Oferta_tryb_ekspresowy_2026-11-21.pdf"
FINDING = f"offer-{SUPPLIER}-{PART}"
ACCEPTANCE = "Karton Nowak accepted the written €16,800 order and released the art books to its logistics team."


def at(clock):
    return dt.datetime.fromisoformat(f"2026-11-22T{clock}:00+01:00")


def seed_offer():
    # The rush offer refers to Annex A. Its €2.10 unit price is recorded on the earlier PO.
    offer = {"supplier_id": SUPPLIER, "material_id": PART, "qty": 8000,
             "valid_until": "2026-11-22", "lead_time_note": "loaded within 4 h of order",
             "transport": "Warta Trans, Poznań – Hamburg about 9 h driving"}
    with closing(sqlite3.connect(rules.DB_PATH)) as db, db:
        db.executescript(ingest.SCHEMA)
        db.execute("INSERT INTO suppliers(supplier_id,name,status,contact_email) VALUES (?,?,'active',?)",
                   (SUPPLIER, "Karton Nowak", SUPPLIER_EMAIL))
        db.execute("INSERT INTO purchase_orders(supplier_id,material_id,unit_price,currency) VALUES (?,?,2.1,'EUR')",
                   (SUPPLIER, PART))
        db.execute("INSERT INTO documents(path,text) VALUES (?,?)", (OFFER_PATH, "Annex A price"))
        db.execute("INSERT INTO doc_facts VALUES (?,'offer',?,'replay')", (OFFER_PATH, json.dumps(offer)))


def email(name, address, body):
    return {"channel": "email", "to_name": name, "to_address": address,
            "language": "en", "subject": "Art book delivery", "body": body}


def propose():
    return actions.propose(
        FINDING, "Order 8,000 art books from Karton Nowak", "Cover the art book shortfall",
        "order", 16800, 708000, 5900, "Print & Packaging", None,
        payload={"commitment": {"delivery_id": DELIVERY, "quantity": 8000,
                                "incoterm": "DAP", "delivery_place": "Hamburg",
                                "emergency_reference": ORDER, "deliver_by": at("12:00").isoformat()}},
        messages=[email("Karton Nowak", SUPPLIER_EMAIL, "8,000 art books, EUR 16,800.")],
    )


def sign(action):
    for person in actions.signers(action):
        approved = actions.decide(action["id"], "approve", person, source="dashboard",
                                  commitment_digest=release.approval_digest(action))
    return approved


def thread(contact, address, question, kind=None, delivery=DELIVERY):
    inquiry = actions.propose(
        FINDING, question, "", "confirm_terms", 0, 0, 0, None, None,
        payload={"delivery_id": delivery, "confirmation_kind": kind},
        messages=[email(contact, address, question)],
    )
    message, = inquiry["messages"]
    outbound._mark(message["id"], "sent")
    return message


def store_reply(message, reply_id, body, summary=None):
    with actions.tx() as db:
        inbox.init_schema(db)
        db.execute("""INSERT INTO replies(id,message_id,msg_id,action_id,from_addr,received_at,body,summary_en,handled)
                      VALUES (?,?,?,?,?,?,?,?,1)""",
                   (reply_id, f"<{reply_id}@replay.example>", message["id"], message["action_id"], message["to_address"],
                    rules.now().isoformat(), body, summary))


def show_evidence(action):
    evidence = action["payload"].get("confirmation_evidence") or release.preview_confirmations(action)
    for line in release.confirmation_summary(evidence):
        print(f"  {line}")


def run(policy, sent):
    with rules.replay(policy, at("00:14")):
        seed_offer()
        release.record_check("files")
        release.record_check("mailbox")
        action = propose()
        terms = release.terms_for(action)
        digest = release.approval_digest(action)
        print(f"{ORDER} · Karton Nowak · 8,000 art books · €16,800 DAP Hamburg")
        print()
        print(release.terms_block(terms))
        print(f"Approval digest: {digest}")
        print()

        allowed, reason = release.check(action)
        assert not allowed
        print(f"00:14 HELD — {reason}")
        show_evidence(action)
        assert outbound.dispatch(sign(action)["id"])["held_reason"].startswith("required condition missing")
        supplier = thread("Karton Nowak", SUPPLIER_EMAIL, "Can you confirm the commercial terms?")
        carrier = thread("Warta Trans", CARRIER_EMAIL, "Can you confirm the booking reference and ETA?", "book_truck")
        dock = thread("Katrin Jansen", DOCK_EMAIL, "Can you confirm the unload window?", "dock_slot")

    with rules.replay(policy, at("02:07")):
        store_reply(supplier, "historical-acceptance", ACCEPTANCE, ACCEPTANCE)
        held = outbound.dispatch(action["id"])
        assert held["status"] == "approved" and "newer supplier reply" in held["held_reason"] and not sent
        print(f"\n02:07 HISTORICAL EVIDENCE — {ACCEPTANCE}")
        print(f"HELD — {held['held_reason']}")
        show_evidence(held)

    with rules.replay(policy, at("02:08")):
        actions.hold(action["id"], "replaced after review of supplier acceptance", supersede=True)
        reviewed = sign(propose())
        assert release.approval_digest(reviewed) == digest
        held = outbound.dispatch(reviewed["id"])
        assert held["status"] == "approved" and not sent
        print(f"\n02:08 BUYER REVIEW AND SIGNATURE — HELD — {held['held_reason']}")
        show_evidence(held)

    with rules.replay(policy, at("02:09")):
        store_reply(carrier, "synthetic-carrier", "Carrier confirmed booking WT-123, ETA Sun 10:30.")
        store_reply(dock, "synthetic-dock", "Dock slot confirmed for Sun 07:00–12:00.")
        released = outbound.dispatch(reviewed["id"])
        assert released["status"] == "sent"
        outbound_email, = sent
        body = outbound_email.get_content()
        assert body == release.render_instruction(terms)[1] + "\n"
        assert {sig["commitment_digest"] for sig in released["signatures"].values()} == {digest}
        assert outbound_email["X-Dockside-Terms-SHA256"] == digest
        print("\n02:09 RELEASED WITH SYNTHETIC EVIDENCE")
        show_evidence(released)
        print(f"X-Dockside-Terms-SHA256: {outbound_email['X-Dockside-Terms-SHA256']}")
        print("Exact outbound body")
        print(body, end="")

        # Rewind the scratch message to pending so the gate evaluates the altered draft as a first send.
        with actions.tx() as db:
            db.execute("UPDATE actions SET status='approved' WHERE id=?", (released["id"],))
            db.execute("UPDATE messages SET body='80,000 art books, EUR 16,800.', status='draft', "
                       "sent_at=NULL, provider_id=NULL WHERE action_id=?", (released["id"],))
        held = outbound.dispatch(released["id"])
        assert held["status"] == "approved" and len(sent) == 1
        print(f"\n02:09 TAMPERED DRAFT (80,000 art books) — HELD — {held['held_reason']}")


def seed_sent_order():
    seed_offer()
    original = propose()
    body = ("We hereby place a formal order for 8,000 finished art books at a total price of EUR 16,800 DAP Hamburg. "
            "Please dispatch via Warta Trans for delivery on Sunday 22 November to DC Hamburg no later than 12:00; "
            "ideal arrival is 07:00. Please use only vehicles with a maximum permitted gross weight of 7.5 t.")
    with actions.tx() as db:
        db.execute("UPDATE actions SET id=?, status='sent', payload='{}' WHERE id=?", (ORDER, original["id"]))
        db.execute("UPDATE messages SET action_id=?, body=?, body_en=?, status='sent', sent_at=? WHERE action_id=?",
                   (ORDER, body, body, rules.now().isoformat(), original["id"]))


def run_revision(policy, sent):
    from core import agent, dispatch, followup, llm_health

    def view():
        with closing(actions.con()) as log:
            related = dispatch.linked_actions(log)[FINDING]
        base = {"FG-GH6-CE": {"projected": 0}}
        alt = {"FG-GH6-CE": {"projected": 2500, "constraintIds": [], "capacityLimited": False}}
        with closing(sqlite3.connect(rules.DB_PATH)) as db:
            offer, = rules.facts(db, "offer")
            route, option = dispatch.recovery(db, offer, {SUPPLIER: "Karton Nowak"}, related, alt, base, at("16:00"), rules.now())
        return route, option

    def show_trace(option, route):
        fields = ("Carrier booking", "Arrival time", "Unload slot", "Vehicle")
        for row in route["steps"]:
            if row["name"] in fields:
                print(f"  {row['name']} [{row['state']}] {row['detail']}")
        print(f"  needsRevisedTerms={str(option['needsRevisedTerms']).lower()}")

    with rules.replay(policy, at("00:14")):
        seed_sent_order()
        carrier = thread("Warta Trans", CARRIER_EMAIL, "Can you confirm the booking, ETA and gross vehicle rating?", "book_truck", ORDER)
        old_slot = thread("Katrin Jansen", DOCK_EMAIL, "Can you confirm the morning unload window?", "dock_slot", ORDER)
        store_reply(old_slot, "old-slot", "Dock slot confirmed for Sun 07:00–12:00.")

    with rules.replay(policy, at("03:20")):
        route, before = view()
        assert before["deliveryState"]["terms_source"] == "sent_order"
        assert before["requestedBy"] == "2026-11-22T12:00+01:00" and before["departForRequested"] == "2026-11-22T03:00+01:00"
        assert before["departBy"] == "2026-11-22T07:00+01:00" and before["releasedAt"] == "2026-11-22T00:14+01:00"
        assert before["needsRevisedTerms"] and not before["deliveryState"]["eta"]
        assert before["deliveryState"]["vehicle_limit"] == 7.5
        assert followup.deadline_for({"option": before, "default_route_id": FINDING}, at("16:00")) == at("07:00")  # a revision only helps while the goods can still leave in time
        question = "Can you confirm which purchase order to use for the art books?"
        staff = actions.propose(FINDING, question, "", "ask", 0, 0, 0, None, None,
                                messages=[dict(email("Paul Winter", None, question), channel="discord")])
        outbound._mark(staff["messages"][0]["id"], "delivered")
        print("\n03:20 HISTORICAL ORDER BEFORE CARRIER REPLY")
        print(before["deliveryState"]["revised_reason"])
        show_trace(before, route)

    with rules.replay(policy, at("03:21")):
        store_reply(carrier, "overweight", "Carrier confirmed booking WT-123, ETA Sun 14:30. Gross vehicle weight is 12 t.")
        _, overweight = view()
        assert not overweight["deliveryState"]["vehicle_ok"]
        assert "gross weight rating at most 7.5 t" in overweight["deliveryState"]["hold_reason"]
        store_reply(carrier, "late-eta", "Carrier confirmed booking WT-123, ETA Sun 14:30. Gross vehicle weight is 7.5 t.")
        route, late = view()
        state = late["deliveryState"]
        assert late["needsRevisedTerms"] and state["booking_ref"] == "WT-123" and state["eta"] == at("14:30").isoformat()
        assert state["vehicle_ok"] and not state["transport_confirmed"] and not state["slot_confirmed"]
        assert "after the ordered 12:00 arrival" in state["revised_reason"]
        print("\n03:21 AUTHORIZED CARRIER REPLY")
        show_trace(late, route)

        snapshot = {"cutoff": at("16:00").isoformat(), "priorityId": policy["priority_product"]["value"],
                    "components": [{"id": PART, "name": "Art books", "status": "short", "usable": 0, "required": 8000,
                                    "next_step": route["next_step"], "routes": [route], "default_route_id": route["id"],
                                    "option": late}]}

        def replay_agent(prompt, on_step=None, *, effort):
            # Exercise the real tool calls using the fields supplied to the model.
            items, _ = json.JSONDecoder().raw_decode(prompt.split("Items:\n", 1)[1])
            item, = items
            act_by = rules.ts(item["act_by"])
            eta = rules.ts(item["verified_eta"])
            window = item["arrival_window"]
            assert act_by == at("07:00")
            assert rules.ts(window["after"]) == at("12:00") and rules.ts(window["latest"]) == at("16:00")
            assert eta == at("14:30") and item["booking_ref"] == "WT-123" and item["vehicle_limit"] == 7.5
            assert item["needs_revised_terms"] and "after the ordered 12:00 arrival" in item["revised_reason"]
            assert item["slot_window"] == {"start": at("07:00").isoformat(), "end": at("12:00").isoformat()}
            assert item["open_staff_questions"] == [{"who": "Paul Winter", "what": question, "asked_at": at("03:20").isoformat()}]

            tools = agent.Tools()
            original = tools.get_action(item["delivery_state"]["action_id"])
            original_terms = original["delivery_terms"]
            arrival = min(eta + dt.timedelta(minutes=30), rules.ts(window["latest"]))
            assert rules.ts(window["after"]) < arrival == at("15:00")
            due = min(15, int((act_by - rules.now()).total_seconds() // 60))
            finding = f"followup-{item['finding_id_for_new_actions']}-{rules.now():%Y%m%d%H%M}"
            draft = [email("Karton Nowak", original_terms["recipient"], "Revised delivery deadline for the existing order.")]
            args = ("Amend the art book delivery deadline", "", item["revised_reason"], "order",
                    original["cost_eur"], original["eur_protected"], original["ce_copies"], original["category"],
                    "Dockside", due, draft)
            tool_spec = next(spec for spec in agent.TOOLS if spec["name"] == "propose_action")
            commitment_fields = tool_spec["parameters"]["properties"]["commitment"]["required"]
            terms = {field: original_terms.get(field) for field in commitment_fields}
            terms.update(deliver_by=arrival.isoformat(), revises_action_id=original["id"])
            result = tools.propose_action(finding, *args, commitment=terms, delivery_id=original_terms["delivery_id"])
            assert result.get("created") and result.get("waiting_for") == "Buyer (Paul Winter)", result
            duplicate = tools.propose_action(f"followup-{FINDING}-202611220322", *args, commitment=terms,
                                            delivery_id=original_terms["delivery_id"])
            assert "already exists" in duplicate["error"]
            recipient, = item["delivery_state"]["contacts"]["dock_slot_confirmed"]
            request = f"Can you confirm an unload slot covering the {eta:%H:%M} arrival by {act_by:%H:%M}?"
            result = tools.propose_action(finding, request, "", "", "confirm_terms", 0, 0, 0, None, "Dockside", due,
                                          [email("Katrin Jansen", recipient, request)],
                                          delivery_id=original_terms["delivery_id"], confirmation_kind="dock_slot")
            assert result.get("done_automatically"), result
            return "Proposed the revised arrival and requested an unload slot."

        with (patch.object(agent, "run", side_effect=replay_agent) as run_followup,
              patch.object(llm_health, "available", return_value=True)):
            assert followup.run(snapshot) == "Proposed the revised arrival and requested an unload slot."
            assert followup.run(snapshot) is None
            run_followup.assert_called_once()

        amended, = actions.list_actions(("proposed",))
        assert actions.signers(amended) == ("Paul Winter",)
        assert release.terms_for(amended)["deliver_by"] == at("15:00").isoformat()
        inquiries = [a for a in actions.list_actions() if a["finding_id"] == amended["finding_id"] and a["kind"] == "confirm_terms"]
        dock_action, = inquiries
        dock, = dock_action["messages"]
        assert dock_action["payload"]["confirmation_kind"] == "dock_slot" and "14:30" in dock["body"]
        assert release.check(amended) == (False, "verified approval for these exact terms is missing")
        print("03:21 FOLLOW-UP PROPOSED — act by 07:00, arrive by 15:00, receiving cutoff 16:00")
        for source in ("files", "mailbox"):
            release.record_check(source)
        held = outbound.dispatch(sign(amended)["id"])
        assert held["status"] == "approved" and "dock slot confirmed" in held["held_reason"]
        print(f"03:21 AMENDMENT SIGNED — HELD — {held['held_reason']}")

    with rules.replay(policy, at("03:22")):
        store_reply(dock, "revised-slot", "Dock slot confirmed for Sun 14:00–15:00.")
        released = outbound.dispatch(amended["id"])
        assert released["status"] == "sent", released["held_reason"]
        outbound_email = sent[-1]
        assert "Do not create another order or booking." in outbound_email.get_content()
        assert "vehicle_limit_t" in outbound_email.get_content()
        route, final = view()
        assert not final["needsRevisedTerms"] and final["deliveryState"]["terms_source"] == "signed_terms"
        assert final["deliveryState"]["slot_confirmed_by"] == "Katrin Jansen"
        assert followup.deadline_for({"option": final, "default_route_id": FINDING}, at("16:00")) == at("15:00")
        print("\n03:22 REVISED TERMS RELEASED — existing order, deliver by Sun 15:00")
        show_trace(final, route)


def main():
    policy = customer.load(ROOT / "config" / "business.json")
    policy["release_carrier_contacts"] = {"value": {DELIVERY: CARRIER_EMAIL, ORDER: CARRIER_EMAIL}}
    policy["staff_email"]["value"]["Katrin Jansen"] = DOCK_EMAIL
    sent = []
    with TemporaryDirectory(prefix="dockside-release-replay-") as directory:
        scratch = Path(directory)
        with (patch.object(actions, "DB", scratch / "actions.db"),
              patch.object(rules, "DB_PATH", scratch / "master.db"),
              patch.dict("os.environ", {"SMTP_HOST": "offline.example", "MAIL_FROM": "dockside@soony.example",
                                       "SMTP_PORT": "587", "SMTP_USER": ""}),
              patch.object(outbound.smtplib, "SMTP") as smtp):
            smtp.return_value.__enter__.return_value.send_message.side_effect = sent.append
            run(policy, sent)
            with (patch.object(actions, "DB", scratch / "revision-actions.db"),
                  patch.object(rules, "DB_PATH", scratch / "revision-master.db"),
                  patch("core.chaser.cases", return_value=([], {}))):
                run_revision(policy, sent)


if __name__ == "__main__":
    main()
