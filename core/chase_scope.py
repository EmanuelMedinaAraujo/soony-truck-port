"""Purchase-order scope shared by Supplier Chaser and Dockside email delivery."""
import json
import re
import sqlite3
from contextlib import closing

from core import rules


def mentions_po(text, po):
    return bool(re.search(r"(?<!\w)" + re.escape(po) + r"(?!\w)", text or "", re.I))


def shipment_finding(finding_id):
    # Follow-up findings wrap the original finding ID and append their timestamp.
    return re.sub(r"^followup-(ship-.+)-\d{12}$", r"\1", finding_id or "")


def covers_po(message, po, finding_id, shipment_orders, *, legacy_details=None):
    raw_scope = message["po_numbers"]
    scope = json.loads(raw_scope) if isinstance(raw_scope, str) else raw_scope
    if scope:
        return po in scope
    return (any(mentions_po(message[field], po) for field in ("subject", "body", "body_en"))
            or po in shipment_orders.get(shipment_finding(finding_id), ())
            or mentions_po(legacy_details, po))


def shipment_orders(c):
    return {f"ship-{sid}": (pos or "").split("|")
            for sid, pos in c.execute("SELECT shipment_id,po_numbers FROM shipments")}


def duplicate_case(c, action, message):
    """Allow the attached Dockside email, and block competing mail on its PO."""
    if not message["to_address"]:
        return None
    candidates = c.execute("""SELECT t.po_number,t.initial_message_id,cc.case_id
        FROM chase_threads t JOIN messages initial ON initial.id=t.initial_message_id
        JOIN chase_cases cc USING(supplier_id,po_number)
        WHERE lower(initial.to_address)=lower(?)
        AND cc.status IN ('open','waiting','answered','escalated')
        AND t.initial_message_id<>? ORDER BY cc.case_id""",
                           (message["to_address"], message["id"])).fetchall()
    if not candidates:
        return None
    shipments = {}
    if not message["po_numbers"] and shipment_finding(action["finding_id"]).startswith("ship-") and rules.DB_PATH.is_file():
        with closing(sqlite3.connect(rules.DB_PATH.as_uri() + "?mode=ro", uri=True)) as master:
            shipments = shipment_orders(master)
    details = None
    if message["po_numbers"] is None and sum(m["channel"] == "email" for m in action["messages"]) == 1:
        details = action["payload"].get("details")
    return next((row["case_id"] for row in candidates
                 if covers_po(message, row["po_number"], action["finding_id"], shipments, legacy_details=details)), None)
