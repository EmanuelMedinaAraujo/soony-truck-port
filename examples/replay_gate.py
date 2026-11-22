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


def thread(contact, address, question, kind=None):
    inquiry = actions.propose(
        FINDING, question, "", "confirm_terms", 0, 0, 0, None, None,
        payload={"delivery_id": DELIVERY, "confirmation_kind": kind},
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


def main():
    policy = customer.load(ROOT / "config" / "business.json")
    policy["release_carrier_contacts"] = {"value": {DELIVERY: CARRIER_EMAIL}}
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


if __name__ == "__main__":
    main()
