"""Release checks for paid actions. Evidence is read again for every pending message."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from core import actions, inbox, rules

CHECKPOINT = """CREATE TABLE IF NOT EXISTS release_evidence (
    source TEXT PRIMARY KEY, checked_at TEXT
)"""
DELIVERY = {"sent", "delivered", "sending", "delivery_unknown"}
CONDITIONS = {
    "transport_confirmed": ("book_truck", r"(?:truck|carrier|transport|pickup) (?:is |has been )?(?:confirmed|booked)"),
    "dock_slot_confirmed": ("dock_slot", r"(?:dock|unload|receiving) slot (?:is |has been )?(?:confirmed|booked)"),
}


def setting(key, default):
    try:
        return rules.cfg(key)
    except KeyError:
        return default


def record_check(source: str, *, valid: bool = True) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).isoformat() if valid else None
    with actions.tx() as log:
        log.execute(CHECKPOINT)
        log.execute("INSERT OR REPLACE INTO release_evidence VALUES (?,?)", (source, stamp))


def _fresh(log) -> bool:
    log.execute(CHECKPOINT)
    stamps = dict(log.execute("SELECT source, checked_at FROM release_evidence"))
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=setting("release_max_age_minutes", 5))
    return all(stamps.get(source) and rules.ts(stamps[source]) >= cutoff for source in ("files", "mailbox"))


def _values(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in value or [] if item]


def _orders(message) -> list[str]:
    value = message.get("po_numbers") or []
    return json.loads(value) if isinstance(value, str) else value


def _match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.I)
    return match.group(0).upper() if match else None


def _master():
    return closing(sqlite3.connect(f"{rules.DB_PATH.resolve().as_uri()}?mode=ro", uri=True))


def _offers(db, supplier, part):
    return [offer for offer in rules.facts(db, "offer")
            if offer.get("supplier_id") == supplier and offer.get("material_id") == part]


def _offer_price(db, offer):
    from core.dispatch import offer_price
    return offer_price(db, offer)[0]


def _latest_offer_release(offer: dict) -> str | None:
    lead = [re.search(r"(\d+(?:\.\d+)?)\s*h\b", offer.get(field) or "", re.I)
            for field in ("lead_time_note", "transport")]
    if not all(lead):
        return None
    cutoff = rules.ts(rules.cfg("deadline")) - dt.timedelta(hours=rules.cfg("receiving_buffer_hours"))
    return (cutoff - dt.timedelta(hours=sum(float(match[1]) for match in lead))).isoformat()


def describe(action: dict, *, parent: dict | None = None, lookup: bool = True) -> dict:
    """Capture terms and derive a stable ID from the underlying purchase scope."""
    payload = action["payload"]
    saved = payload.get("commitment") or {}
    origin = parent["finding_id"] if parent else action["finding_id"]
    inherited = parent["payload"].get("commitment", {}) if parent else {}
    messages = [m for m in action["messages"] if m["channel"] != "approval_email"]
    addresses = sorted({m["to_address"].casefold() for m in messages if m.get("to_address")})
    part = (saved.get("part") or payload.get("part") or payload.get("material_id")
            or _match(actions.PART.pattern, origin or "") or inherited.get("part"))
    supplier = (saved.get("supplier_id") or payload.get("supplier_id")
                or _match(r"SUP-\d+", origin or "") or inherited.get("supplier_id"))
    po = sorted(set(_values(saved.get("po") or payload.get("po_number"))
                    or [p for message in messages for p in _orders(message)]
                    or _values(inherited.get("po"))))
    qty = saved.get("quantity") or payload.get("quantity")
    price = saved.get("price_eur") or payload.get("unit_price_eur") or payload.get("price_eur")
    valid = saved.get("valid_until") or payload.get("valid_until") or payload.get("validUntil")
    latest = saved.get("latest_useful_at") or payload.get("latestUsefulAt") or payload.get("latest_useful_at")
    evidence_path = saved.get("offer_path")
    if lookup and rules.DB_PATH.is_file():
        try:
            with _master() as db:
                if not supplier and addresses:
                    matches = {sid for sid, address in db.execute("SELECT supplier_id, contact_email FROM suppliers")
                               if (address or "").casefold() in addresses}
                    if len(matches) == 1:
                        supplier = matches.pop()
                offers = _offers(db, supplier, part) if action["kind"] == "order" and supplier and part else []
                if len(offers) == 1:
                    offer = offers[0]
                    evidence_path = evidence_path or offer["_path"]
                    qty = qty or offer.get("qty")
                    valid = valid or offer.get("valid_until")
                    if qty and price is None:
                        price = round(action["cost_eur"] / qty, 6)
                    latest = latest or _latest_offer_release(offer)
        except (sqlite3.Error, ValueError, TypeError):
            pass  # The gate checks evidence availability before any send.
    conditions = set(_values(saved.get("conditions") or payload.get("conditions")))
    same_kind = parent and parent["kind"] == action["kind"]
    scope = (action["kind"], supplier or addresses, part or po or re.sub(r"^(?:ship|track)-", "", origin or ""))
    key = saved.get("key") or (inherited.get("key") if same_kind else None)
    key = key or "COM-" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()[:20].upper()
    return {"key": key, "supplier_id": supplier, "part": part, "quantity": qty, "price_eur": price,
            "po": po[0] if len(po) == 1 else po, "valid_until": valid, "latest_useful_at": latest,
            "conditions": sorted(conditions), "offer_path": evidence_path,
            "parent_action_id": parent["id"] if parent else saved.get("parent_action_id")}


def _commitment(action: dict) -> dict:
    return action["payload"].get("commitment") or describe(action)


def _different(expected, observed) -> bool:
    if expected is None or observed is None:
        return False
    return not math.isclose(float(expected), float(observed), rel_tol=1e-7, abs_tol=1e-6)


def _expired(terms: dict) -> bool:
    now = rules.now()
    if valid := terms.get("valid_until"):
        if len(valid) == 10:
            if now.date() > dt.date.fromisoformat(valid):
                return True
        elif now >= rules.ts(valid):
            return True
    return bool(terms.get("latest_useful_at") and now >= rules.ts(terms["latest_useful_at"]))


def _scope(terms, candidate) -> bool:
    po = set(_values(terms.get("po")))
    other = set(_values(candidate.get("po")))
    if terms.get("part") and candidate.get("part") and terms["part"] != candidate["part"]:
        return False
    if po and other:
        return bool(po & other)
    return bool(terms.get("part") and terms["part"] == candidate.get("part") and
                terms.get("supplier_id") and terms["supplier_id"] == candidate.get("supplier_id"))


def _related_replies(log, action, terms):
    inbox.init_schema(log)
    recipients = {m["to_address"].casefold() for m in action["messages"]
                  if m.get("to_address") and m["channel"] != "approval_email"}
    conditions = set(terms["conditions"]) | set(setting("release_conditions", {}).get(action["kind"], []))
    confirmed = {}
    parent_id = terms.get("parent_action_id")
    for reply in log.execute("SELECT * FROM replies WHERE simulated=0 ORDER BY julianday(received_at)"):
        origin = actions._get(log, reply["action_id"]) if reply["action_id"] else None
        if (reply["from_addr"] or "").casefold() in recipients:
            if not reply["handled"]:
                return "an unread supplier reply needs review"
            if rules.ts(reply["received_at"]) >= rules.ts(action["created_at"]):
                return "newer supplier reply needs review of quantity, price and PO"
        if origin is None or not reply["handled"]:
            continue
        origin_terms = origin["payload"].get("commitment") or describe(origin, lookup=False)
        same_finding = origin["finding_id"] == action["finding_id"] or origin["id"] == parent_id
        if not (same_finding or _scope(terms, origin_terms)):
            continue
        if not any(m["id"] == reply["msg_id"] and m["status"] in DELIVERY
                   and (m["to_address"] or "").casefold() == (reply["from_addr"] or "").casefold()
                   for m in origin["messages"]):
            continue
        for condition in conditions.intersection(CONDITIONS):
            kind, pattern = CONDITIONS[condition]
            if origin["kind"] == kind:
                # The latest correlated reply has to explicitly confirm the requested arrangement.
                confirmed[condition] = bool(re.match(pattern + r"(?:[.!]|$)", (reply["body"] or "").strip(), re.I)
                                            and not re.search(r"\b(?:cancel\w*|unavailable|cannot|not|but|pending)\b",
                                                              reply["body"], re.I))
    for condition in sorted(conditions):
        if not confirmed.get(condition):
            return f"required condition missing: {condition.replace('_', ' ')}"
    return None


def _file_blocker(action: dict, terms: dict) -> str | None:
    supplier, part = terms["supplier_id"], terms["part"]
    po = _values(terms["po"])
    if reason := rules.shipment_commitment_blocker(action["finding_id"], action["kind"], po):
        return reason
    with _master() as db:
        status = db.execute("SELECT status FROM suppliers WHERE supplier_id=?", (supplier,)).fetchone() if supplier else None
        if supplier and (not status or (status[0] or "").casefold() != "active"):
            return "supplier is missing or inactive in the latest files"
        if action["kind"] == "order" and supplier:
            contact = db.execute("SELECT contact_email FROM suppliers WHERE supplier_id=?", (supplier,)).fetchone()[0]
            if any(m["channel"] == "email" and (m["to_address"] or "").casefold() != (contact or "").casefold()
                   for m in action["messages"]):
                return "recipient disagrees with the latest supplier contact"
        draft_pos = {p for message in action["messages"] for p in _orders(message)}
        if draft_pos and draft_pos != set(po):
            return "draft references disagree with the approved PO"
        for order in po:
            rows = db.execute("""SELECT supplier_id, material_id, quantity, unit_price, currency, status
                                 FROM purchase_orders WHERE po_number=?""", (order,)).fetchall()
            matching = [row for row in rows if (not supplier or row[0] == supplier) and (not part or row[1] == part)]
            if not matching:
                return f"PO {order} is missing or disagrees with the supplier or part"
            if any((row[5] or "").casefold() in {"cancelled", "canceled", "closed"} for row in matching):
                return f"PO {order} is closed or cancelled"
            if action["kind"] == "order" and (_different(terms["quantity"], sum(row[2] or 0 for row in matching)) or
                    any(row[4] != "EUR" or _different(terms["price_eur"], row[3]) for row in matching)):
                return f"PO {order} disagrees on quantity or price"
        offers = _offers(db, supplier, part) if action["kind"] == "order" and supplier and part else []
        if action["kind"] == "order" and not (supplier and part and terms["quantity"] and terms["price_eur"] and (offers or po)):
            return "order quantity, price, supplier or source evidence is missing"
        if action["kind"] == "order":
            if len([m for m in action["messages"] if m["channel"] == "email"]) > 1:
                return "a purchase order must have a single supplier delivery"
            if _different(action["cost_eur"], round(float(terms["price_eur"]) * float(terms["quantity"]), 2)):
                return "approved cost disagrees with the approved price and quantity"
        if terms.get("offer_path") and not any(offer["_path"] == terms["offer_path"] for offer in offers):
            return "the approved offer is missing from the latest files"
        for offer in offers:
            price = _offer_price(db, offer)
            if offer.get("qty") is None or _different(terms["quantity"], offer["qty"]):
                return f"supplier offer disagrees on quantity: {Path(offer['_path']).name}"
            if price is None or _different(terms["price_eur"], price):
                return f"supplier offer disagrees on price: {Path(offer['_path']).name}"
            if offer.get("po_number") and po and offer["po_number"] not in po:
                return "supplier offer disagrees on PO"
            if _expired({"valid_until": offer.get("valid_until"), "latest_useful_at": _latest_offer_release(offer)}):
                return "supplier offer or latest useful release time has expired"
        for kind in ("supplier_hold", "delivery_promise", "shipping_advice", "receipt_correction", "commercial_term"):
            for fact in rules.facts(db, kind):
                fact_po = fact.get("po_number")
                if fact.get("supplier_id") and supplier and fact["supplier_id"] != supplier:
                    continue
                if fact.get("material_id") and part and fact["material_id"] != part:
                    continue
                changed = db.execute("SELECT last_changed FROM sources WHERE path=?", (fact["_path"],)).fetchone()
                if not changed or not ((fact_po and fact_po in po) or (
                        fact.get("supplier_id") == supplier and supplier and fact.get("material_id") == part and part
                        and changed[0] and rules.ts(changed[0]) >= rules.ts(action["created_at"]))):
                    continue
                if kind == "supplier_hold":
                    return f"supplier hold in {Path(fact['_path']).name}"
                quantity = next((fact[field] for field in ("qty_corrected", "qty_shipped", "qty")
                                 if fact.get(field) is not None), None)
                if (_different(terms["quantity"], quantity) or _different(terms["price_eur"], fact.get("unit_price_eur"))
                        or (po and fact_po and fact_po not in po)):
                    return f"document evidence disagrees on quantity, price or PO: {Path(fact['_path']).name}"
    return None


def scheduled_authorized(log, action) -> bool:
    if (action["cost_eur"] <= 0 or not actions.policy_schedules(action) or not action["due_at"]
            or rules.now() < rules.ts(action["due_at"])):
        return False
    from core import approvals
    (name,) = actions.signers(action)
    return approvals.notified(log, action["id"], name)


def check(action: dict) -> tuple[bool, str | None]:
    """Check authorization, idempotency and current evidence immediately before release."""
    if action["status"] not in {"scheduled", "approved", "sent"}:
        return False, "action is not authorized for release"
    try:
        terms = _commitment(action)
        with actions.tx() as log:
            for row in log.execute("""SELECT id FROM actions WHERE id<>? AND kind=? AND cost_eur>0 AND (
                status IN ('sent','done') OR EXISTS(SELECT 1 FROM messages m WHERE m.action_id=actions.id
                AND m.channel<>'approval_email' AND m.status IN ('sent','delivered','sending','delivery_unknown')))""",
                                   (action["id"], action["kind"])):
                other = actions._get(log, row["id"])
                if _commitment(other)["key"] == terms["key"]:
                    return False, f"duplicate of {other['id']}; the commitment already went out or delivery is unconfirmed"
            required = set(actions.signers(action))
            if action["status"] == "scheduled":
                authorized = scheduled_authorized(log, action)
            else:
                authorized = required and required <= action["signatures"].keys()
            if not authorized:
                return False, "verified approval or the notified stop window is missing"
            if _expired(terms):
                return False, "commitment or latest useful release time has expired"
            if not _fresh(log):
                return False, "evidence stale"
            if not action["payload"].get("commitment"):
                return False, "approved commitment terms are missing; a new proposal is needed"
            if reason := _related_replies(log, action, terms):
                return False, reason
        reason = _file_blocker(action, terms)
        return reason is None, reason
    except (sqlite3.Error, ValueError, TypeError):
        return False, "evidence stale or unreadable"
