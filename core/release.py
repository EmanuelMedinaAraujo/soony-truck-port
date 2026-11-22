"""Typed supplier instructions and release checks against current evidence."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from contextlib import closing
from decimal import Decimal
from email.utils import parseaddr
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
INSTRUCTIONS = {"order": "Purchase and dispatch", "dispatch": "Dispatch", "expedite": "Expedite dispatch",
                "book_truck": "Book freight", "dock_slot": "Book a dock slot"}
LOCAL_INSTRUCTIONS = {
    "de": {"order": "Bestellung und Versand", "dispatch": "Versand freigeben", "expedite": "Versand beschleunigen",
           "book_truck": "Fracht buchen", "dock_slot": "Entladezeitfenster buchen"},
    "pl": {"order": "Zamówienie i wysyłka", "dispatch": "Zlecenie wysyłki", "expedite": "Przyspieszenie wysyłki",
           "book_truck": "Zlecenie transportu", "dock_slot": "Rezerwacja okna rozładunkowego"},
    "ko": {"order": "주문 및 발송", "dispatch": "발송 요청", "expedite": "긴급 발송 요청",
           "book_truck": "운송 예약", "dock_slot": "하역 시간 예약"},
    "zh": {"order": "采购及发货", "dispatch": "发货指令", "expedite": "加急发货",
           "book_truck": "安排运输", "dock_slot": "预约卸货时间"},
}
LOCAL_REPEAT = {
    "de": "Wiederholung der bestehenden Anweisung. Keine zusätzliche Bestellung oder Buchung anlegen.",
    "pl": "Powtórzenie istniejącego zlecenia. Prosimy nie tworzyć dodatkowego zamówienia ani rezerwacji.",
    "ko": "기존 지시를 다시 전달합니다. 추가 주문이나 예약을 생성하지 마십시오.",
    "zh": "重发原指令。请勿创建额外订单或预约。",
}
INTENT = {
    "order": r"\b(?:order|purchase|bestell\w*|zam[oó]w\w*|commander|commande|orden de compra|pedido)\b|주문|采购|採購|下单|下單|訂購|订购",
    "dispatch": r"\b(?:dispatch(?:ed|ing)?|ship(?:ping|ped)?|release|versend\w*|verschick\w*|freigeb\w*|wysył\w*|wysłać|wyślij\w*|nadaj\w*|expédier|expédition|enviar|despach\w*)\b|발송|출고|배송|发货|發貨|出货|出貨|发运|發運",
    "book_truck": r"\b(?:book(?:ing)? (?:a |the )?(?:truck|carrier|freight|transport|pickup)|freight booking|book(?:ing)?|buchen|buchung|beauftrag\w*|zarezerwuj\w*|rezerwacj\w*)\b|운송 예약|트럭 예약|订车|訂車|订舱|訂艙|安排运输|安排運輸",
}
DIRECTIVE = re.compile(
    r"\b(?:please\s+(?:now\s+)?(?:order|purchase|dispatch|ship|release|send|book|proceed)|"
    r"(?:could|can|would|will) you\s+(?:please\s+)?(?:order|purchase|dispatch|ship|release|send|book)\b|"
    r"(?:need|want|ask|require) you to\s+(?:order|purchase|dispatch|ship|release|send|book)\b|"
    r"(?:go ahead|proceed) with|(?:we|i) (?:hereby |now )?(?:order|purchase|accept|confirm)\b|"
    r"wir bestellen|hiermit bestellen|bitte (?:bestell|versend|verschick|buch|beauftrag)|"
    r"proszę (?:o )?(?:zam[oó]w|wys[ły]|wyślij|nad|zarezerw)|zamawiamy|zlecenie wysyłki|"
    r"veuillez (?:commander|expédier)|por favor (?:enviar|despachar))|"
    r"주문합니다|발송해|출고해|배송해|예약해|请(?:发货|发运|订车|订舱|采购|安排运输)|請(?:發貨|發運|訂車|訂艙|採購|安排運輸)", re.I
)
INQUIRY = re.compile(
    r"^\s*(?:please (?:confirm|advise|share|tell|provide|let)|could you (?:confirm|advise|share|tell|provide)|"
    r"can you (?:confirm|advise|share|tell|provide)|(?:what|when|whether|where|is|has|was)\b|"
    r"bitte (?:bestätigen|teilen)|können sie (?:bestätigen|mitteilen)|"
    r"prosimy o (?:potwierdzenie|informację)|czy (?:można|potwierdzą)|"
    r"请确认|請確認|请告知|請告知|확인 부탁|확인해)", re.I
)
NUMBER = r"[0-9]+(?:[.,'’ ][0-9]+)*"
CURRENCY = r"EUR|USD|PLN|KRW|CNY|RMB|GBP|JPY|CHF|CAD|AUD|€|\$|£|¥|₩|zł|元|원"
PRICE = re.compile(rf"(?<!\w)(?:({CURRENCY})\s*({NUMBER})|({NUMBER})\s*({CURRENCY}))(?![a-z])", re.I)
QUANTITY = re.compile(
    rf"(?<![\w.,-])({NUMBER})(?=\s*(?:units?|pcs?\.?|copies|books?|art books?|pieces?|"
    r"cases?|pallets?|cartons?|boxes|items?|exemplaires?|unidades?|stück|stk\.?|einheiten|sztuk\w*|szt\.?|"
    r"개|권|부|件|册|冊|套|箱)(?![a-z]))", re.I
)
QUANTITY_LABEL = re.compile(rf"(?:\b(?:qty|quantity|menge|ilość)\b|数量|수량)\s*[:=]?\s*({NUMBER})", re.I)
UNIT_PRICE = re.compile(rf"\b(?:unit price|stückpreis|cena jednostkowa)\s*[:=]?\s*(?:{CURRENCY}\s*)?({NUMBER})", re.I)
TOTAL_PRICE = re.compile(rf"\b(?:total price|gesamtpreis|cena całkowita)\s*[:=]?\s*(?:{CURRENCY}\s*)?({NUMBER})", re.I)
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}")
INCOTERMS = {"EXW", "FCA", "CPT", "CIP", "DAP", "DPU", "DDP", "FAS", "FOB", "CFR", "CIF"}
TERMS_VERSION = 1


def _normalized(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "")


def instruction_kind(kind: str, messages) -> str | None:
    """Classify the actual subject, message and supplied translation independently of cost."""
    if kind in INSTRUCTIONS and kind != "dock_slot" and messages:
        return kind
    for message in messages:
        if message["channel"] == "approval_email":
            continue
        # Each line/clause is evaluated separately so an inquiry cannot mask a following directive.
        for field in ("body", "body_en", "subject"):
            for line in re.split(r"[\n;!?。！？]+|(?<!\d)\.(?=\s)", _normalized(message.get(field))):
                for intent, pattern in INTENT.items():
                    if not re.search(pattern, line, re.I):
                        continue
                    if DIRECTIVE.search(line):
                        return intent
                    if INQUIRY.search(line):
                        break
                    if QUANTITY.search(line) and PRICE.search(line):
                        return intent
                    if field != "subject" and re.match(rf"\s*(?:{pattern})(?:\s|$|:)", line, re.I):
                        return intent
                    if field == "subject" and re.fullmatch(rf"\s*(?:{pattern})\s*", line, re.I):
                        return intent
                if QUANTITY.search(line) and PRICE.search(line) and not INQUIRY.search(line):
                    return "order"
    return None


def applies(action: dict) -> bool:
    messages = [m for m in action["messages"] if m["channel"] != "approval_email"]
    return bool(messages and (action["cost_eur"] != 0 or action["payload"].get("commitment")
                              or action["payload"].get("repeat_commitment")
                              or instruction_kind(action["kind"], messages)))


def terms_block(terms: dict) -> str:
    return "Dockside commitment\n" + json.dumps(terms, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def fingerprint(terms: dict) -> str:
    return hashlib.sha256(terms_block(terms).encode()).hexdigest()


def terms_for(action: dict) -> dict | None:
    terms = action["payload"].get("commitment")
    return terms if isinstance(terms, dict) and terms.get("version") == TERMS_VERSION else None


def render_instruction(terms: dict, repeat: bool = False) -> tuple[str, str]:
    verb = INSTRUCTIONS[terms["instruction"]]
    key = terms["commitment_id"]
    subject = f"{'Repeat · ' if repeat else ''}{verb} · {key}"
    opening = (f"Repeat of {key}. Carry out the existing instruction below. Do not create an additional order or booking."
               if repeat else f"{verb} according to these terms.")
    if local := LOCAL_INSTRUCTIONS.get(terms["language"]):
        verb_local = local[terms["instruction"]]
        subject = f"{verb_local} · {subject}"
        opening = f"{LOCAL_REPEAT[terms['language']] if repeat else verb_local + '.'}\n{opening}"
    return subject, f"{opening}\n\n{terms_block(terms)}"


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


def describe(action: dict, *, parent: dict | None = None) -> dict:
    """Capture an instruction from provided terms and uniquely identifiable source data."""
    payload = action["payload"]
    saved = payload.get("commitment") or {}
    finding = (parent["finding_id"] if parent else action["finding_id"]) or ""
    messages = [m for m in action["messages"] if m["channel"] != "approval_email"]
    addresses = sorted({m["to_address"].casefold() for m in messages if m.get("to_address")})
    language = (messages[0].get("language") or "en").lower().split("-")[0] if messages else "en"
    instruction = instruction_kind(action["kind"], messages) or action["kind"]
    part = saved.get("part") or payload.get("part") or payload.get("material_id") or _match(actions.PART.pattern, finding)
    supplier = saved.get("supplier_id") or payload.get("supplier_id") or _match(r"SUP-\d+", finding)
    orders = _values(saved.get("po") or payload.get("po_number")) or sorted({p for m in messages for p in _orders(m)})
    po = orders[0] if len(orders) == 1 else None
    po_line, emergency = saved.get("po_line"), saved.get("emergency_reference")
    quantity = saved.get("quantity") if saved.get("quantity") is not None else payload.get("quantity")
    price = saved.get("unit_price")
    if price is None:
        price = saved.get("price_eur", payload.get("unit_price_eur"))
    total = saved.get("total_price")
    currency = saved.get("currency") or ("EUR" if saved.get("price_eur") is not None or
                                         payload.get("unit_price_eur") is not None else None)
    incoterm, place, deliver_by = (saved.get(field) for field in ("incoterm", "delivery_place", "deliver_by"))
    valid = saved.get("valid_until") or payload.get("valid_until") or payload.get("validUntil")
    latest = saved.get("latest_useful_at") or payload.get("latestUsefulAt") or payload.get("latest_useful_at")
    if rules.DB_PATH.is_file():
        try:
            with _master() as db:
                if not supplier and addresses:
                    matches = {sid for sid, address in db.execute("SELECT supplier_id, contact_email FROM suppliers")
                               if (address or "").casefold() in addresses}
                    if len(matches) == 1:
                        supplier = matches.pop()
                if po:
                    rows = db.execute("""SELECT po_line, quantity, unit_price, currency, incoterm, plant, requested_date
                                         FROM purchase_orders WHERE po_number=? AND supplier_id=? AND material_id=?
                                         AND (? IS NULL OR po_line=?)""", (po, supplier, part, po_line, po_line)).fetchall()
                    if len(rows) == 1:
                        line, line_qty, line_price, line_currency, term, plant, requested = rows[0]
                        po_line = line
                        incoterm, place, deliver_by = incoterm or term, place or plant, deliver_by or requested
                        if instruction in {"order", "dispatch"}:
                            quantity = line_qty if quantity is None else quantity
                            price = line_price if price is None else price
                            currency = currency or line_currency
                offers = _offers(db, supplier, part) if instruction == "order" and supplier and part and not po else []
                if len(offers) == 1:
                    offer = offers[0]
                    quantity = offer.get("qty") if quantity is None else quantity
                    if price is None:
                        price = _offer_price(db, offer)
                        if price is not None:
                            currency = "EUR"
                    valid = valid or offer.get("valid_until")
                    latest = latest or _latest_offer_release(offer)
        except (sqlite3.Error, ValueError, TypeError):
            pass
    if total is None and isinstance(price, (float, int)) and isinstance(quantity, (float, int)):
        total = round(price * quantity, 2)
    conditions = set(_values(saved.get("conditions") or payload.get("conditions")))
    conditions.update(setting("release_conditions", {}).get(instruction, []))
    scope = (instruction, supplier or addresses, part, po or emergency or finding, po_line)
    key = "COM-" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()[:20].upper()
    delivery_id = saved.get("delivery_id") or payload.get("delivery_id") or key
    return {"version": TERMS_VERSION, "commitment_id": key, "instruction": instruction, "delivery_id": delivery_id,
            "supplier_id": supplier, "recipient": addresses[0] if len(addresses) == 1 else None,
            "language": language if language in LOCAL_INSTRUCTIONS else "en",
            "part": part, "quantity": quantity, "unit_price": price, "total_price": total,
            "currency": currency, "incoterm": incoterm, "delivery_place": place, "po": po, "po_line": po_line,
            "emergency_reference": emergency, "deliver_by": deliver_by, "valid_until": valid,
            "latest_useful_at": latest, "conditions": sorted(conditions)}


def delivery_reference(delivery_id: str) -> str | None:
    if isinstance(delivery_id, str) and REF.fullmatch(delivery_id):
        return f"Delivery reference: {delivery_id}"
    return None


def _number_candidates(raw: str) -> set[Decimal]:
    clean = re.sub(r"[ '’]", "", raw)
    if "," in clean and "." in clean:
        decimal = "," if clean.rfind(",") > clean.rfind(".") else "."
        return {Decimal(clean.replace("." if decimal == "," else ",", "").replace(decimal, "."))}
    pieces = re.split(r"[.,]", clean)
    values = {Decimal("".join(pieces))} if all(len(p) == 3 for p in pieces[1:]) else set()
    if len(pieces) == 2:
        values.add(Decimal(".".join(pieces)))
    return values


def _body_blocker(terms, message):
    qty, unit, total = (Decimal(str(terms[k])) for k in ("quantity", "unit_price", "total_price"))
    currencies = {"€": "EUR", "$": "USD", "£": "GBP", "₩": "KRW", "ZŁ": "PLN", "RMB": "CNY",
                  "元": "CNY", "원": "KRW", "¥": terms["currency"] if terms["currency"] in {"JPY", "CNY"} else ""}
    for field in ("body", "body_en", "subject"):
        content = _normalized(message.get(field))
        for match in PRICE.finditer(content):
            prefix, left, right, suffix = match.groups()
            symbol = (prefix or suffix).upper()
            if currencies.get(symbol, symbol) != terms["currency"] or not ({unit, total} & _number_candidates(left or right)):
                return "draft price or currency disagrees with the commitment"
        for match in (*QUANTITY.finditer(content), *QUANTITY_LABEL.finditer(content)):
            if qty not in _number_candidates(match[1]):
                return "draft quantity disagrees with the commitment"
        for pattern, expected in ((UNIT_PRICE, unit), (TOTAL_PRICE, total)):
            if any(expected not in _number_candidates(match[1]) for match in pattern.finditer(content)):
                return "draft price disagrees with the commitment"
    return None


def proposal_blocker(action: dict) -> str | None:
    terms = terms_for(action)
    if not terms:
        return "typed commitment is missing"
    try:
        if _expired(terms):
            return "commitment or latest useful release time has expired"
        if terms["instruction"] not in INSTRUCTIONS:
            return "commitment instruction type is missing"
        numeric = (terms.get(key) for key in ("quantity", "unit_price", "total_price"))
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in numeric):
            return "commitment quantity, unit price and total price are required"
        if not terms["quantity"] or _different(terms["total_price"], round(terms["quantity"] * terms["unit_price"], 2)):
            return "commitment quantity and prices do not reconcile"
        if (not all(isinstance(terms.get(k), str) and REF.fullmatch(terms[k])
                    for k in ("commitment_id", "part", "supplier_id", "delivery_id"))
                or not re.fullmatch(r"[A-Z]{3}", terms["currency"] or "")
                or terms["incoterm"] not in INCOTERMS or not isinstance(terms["delivery_place"], str)
                or not 0 < len(terms["delivery_place"].strip()) <= 120
                or any(ord(char) < 32 for char in terms["delivery_place"])
                or not terms["deliver_by"] or not terms["valid_until"]):
            return "complete the supplier, part, currency, Incoterm, delivery place, deliver-by and expiry"
        rules.ts(terms["deliver_by"])
        po, line, emergency = (terms.get(key) for key in ("po", "po_line", "emergency_reference"))
        if po:
            valid_ref = isinstance(po, str) and REF.fullmatch(po) and type(line) is int and line > 0 and not emergency
        else:
            valid_ref = isinstance(emergency, str) and REF.fullmatch(emergency) and line is None
        if not valid_ref:
            return "commitment needs one PO number and line or one emergency reference"
        if set(terms["conditions"]) - CONDITIONS.keys():
            return "commitment has an unsupported release condition"
        messages = [m for m in action["messages"] if m["channel"] != "approval_email"]
        if (len(messages) != 1 or messages[0]["channel"] != "email" or not terms["recipient"]
                or "@" not in terms["recipient"] or parseaddr(terms["recipient"])[1] != terms["recipient"]):
            return "commitment requires a single email to its approved recipient"
        message, = messages
        if (message["to_address"] or "").casefold() != terms["recipient"]:
            return "draft recipient disagrees with the commitment"
        if _orders(message) and set(_orders(message)) != ({po} if po else set()):
            return "draft references disagree with the approved PO"
        if reason := _body_blocker(terms, message):
            return reason
        if action["payload"].get("commitment_digest") != fingerprint(terms):
            return "commitment terms changed; a new proposal is needed"
        if not action["payload"].get("repeat_commitment"):
            existing_po = terms["instruction"] == "dispatch" and po and action["cost_eur"] == 0
            if not existing_po and terms["currency"] == "EUR" and _different(action["cost_eur"], terms["total_price"]):
                return "approved cost disagrees with the approved total price"
            if not existing_po and terms["currency"] != "EUR" and terms["total_price"] and action["cost_eur"] <= 0:
                return "record the evidenced EUR equivalent for approval routing"
    except (ArithmeticError, AttributeError, KeyError, ValueError, TypeError):
        return "commitment terms are malformed"
    return None


def approval_digest(action: dict) -> str | None:
    return action["payload"].get("commitment_digest") if terms_for(action) else None


def _signed(action, digest):
    required = set(actions.signers(action))
    return bool(required and all(action["signatures"].get(name, {}).get("commitment_digest") == digest for name in required))


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
    if deadline := terms.get("deliver_by"):
        if (now.date() > dt.date.fromisoformat(deadline) if len(deadline) == 10 else now >= rules.ts(deadline)):
            return True
    return bool(terms.get("latest_useful_at") and now >= rules.ts(terms["latest_useful_at"]))


def _related_replies(log, action, terms):
    inbox.init_schema(log)
    recipient = terms["recipient"]
    delivery_id = terms["delivery_id"]
    conditions = set(terms["conditions"])
    if set(setting("release_conditions", {}).get(terms["instruction"], [])) - conditions:
        return "required release conditions changed since approval"
    confirmed = {}
    for reply in log.execute("SELECT * FROM replies WHERE simulated=0 ORDER BY julianday(received_at), rowid"):
        sender = (reply["from_addr"] or "").casefold()
        origin = actions._get(log, reply["action_id"]) if reply["action_id"] else None
        if sender == recipient:
            if not reply["handled"]:
                return "an unread supplier reply needs review"
            if rules.ts(reply["received_at"]) >= rules.ts(action["created_at"]):
                return "newer supplier reply needs review of quantity, price and PO"
        if origin is None:
            continue
        origin_terms = terms_for(origin)
        if origin_terms:
            same_delivery = origin_terms["delivery_id"] == delivery_id
        else:
            reference = delivery_reference(delivery_id)
            same_delivery = reference and origin["payload"].get("delivery_id") == delivery_id and any(
                m["id"] == reply["msg_id"] and m["body"].endswith("\n\n" + reference) for m in origin["messages"])
        if not same_delivery or not any(m["id"] == reply["msg_id"] and m["status"] in {"sent", "delivered"}
                                        and (m["to_address"] or "").casefold() == sender for m in origin["messages"]):
            continue
        for condition in conditions.intersection(CONDITIONS):
            kind, pattern = CONDITIONS[condition]
            if (origin["kind"] == kind or (origin_terms and origin_terms["instruction"] == kind)
                    or (origin["kind"] == "confirm_terms" and origin["payload"].get("confirmation_kind") == kind)):
                confirmed[condition] = bool(reply["handled"] and re.match(pattern + r"(?:[.!]|$)", (reply["body"] or "").strip(), re.I)
                                            and not re.search(r"\b(?:cancel\w*|unavailable|cannot|not|but|pending)\b",
                                                              reply["body"], re.I))
    for condition in sorted(conditions):
        if not confirmed.get(condition):
            return f"required condition missing: {condition.replace('_', ' ')}"
    return None


def _file_blocker(action: dict, terms: dict) -> str | None:
    supplier, part = terms["supplier_id"], terms["part"]
    po = _values(terms["po"])
    instruction = terms["instruction"]
    shipment_kind = "order" if instruction == "dispatch" else instruction
    if reason := rules.shipment_commitment_blocker(f"ship-{terms['delivery_id']}", shipment_kind, po):
        return reason
    if reason := rules.shipment_commitment_blocker(action["finding_id"], shipment_kind, po):
        return reason
    with _master() as db:
        status = db.execute("SELECT status FROM suppliers WHERE supplier_id=?", (supplier,)).fetchone() if supplier else None
        if supplier and (not status or (status[0] or "").casefold() != "active"):
            return "supplier is missing or inactive in the latest files"
        if supplier:
            contact = db.execute("SELECT contact_email FROM suppliers WHERE supplier_id=?", (supplier,)).fetchone()[0]
            if any(m["channel"] == "email" and (m["to_address"] or "").casefold() != (contact or "").casefold()
                   for m in action["messages"]):
                return "recipient disagrees with the latest supplier contact"
        for order in po:
            rows = db.execute("""SELECT supplier_id, material_id, quantity, unit_price, currency, status,
                                        incoterm, plant, requested_date
                                 FROM purchase_orders WHERE po_number=? AND po_line=?""",
                              (order, terms["po_line"])).fetchall()
            matching = [row for row in rows if row[1] == part and (instruction not in {"order", "dispatch"} or row[0] == supplier)]
            if len(matching) != 1:
                return f"PO {order} line {terms['po_line']} is missing or disagrees with the supplier or part"
            _, _, qty, unit, currency, status, incoterm, place, due = matching[0]
            if (status or "").casefold() in {"cancelled", "canceled", "closed"}:
                return f"PO {order} line {terms['po_line']} is closed or cancelled"
            if qty is None or _different(terms["quantity"], qty) or (instruction in {"order", "dispatch"} and (
                    unit is None or _different(terms["unit_price"], unit) or terms["currency"] != currency)):
                return f"PO {order} line {terms['po_line']} disagrees on quantity or price"
            if instruction in {"order", "dispatch"} and any(value and value != terms[field] for field, value in (
                    ("incoterm", incoterm), ("delivery_place", place), ("deliver_by", due))):
                return f"PO {order} line {terms['po_line']} disagrees on delivery terms"
        offers = [offer for offer in _offers(db, supplier, part)
                  if not offer.get("po_number") or offer["po_number"] in po] if instruction == "order" else []
        if instruction == "order" and not po and not offers:
            return "the emergency purchase needs a current supplier offer"
        for offer in offers:
            price = _offer_price(db, offer)
            if offer.get("qty") is None or _different(terms["quantity"], offer["qty"]):
                return f"supplier offer disagrees on quantity: {Path(offer['_path']).name}"
            if price is None or terms["currency"] != "EUR" or _different(terms["unit_price"], price):
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
                if (_different(terms["quantity"], quantity) or (terms["currency"] == "EUR" and _different(terms["unit_price"], fact.get("unit_price_eur")))
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


def _duplicate(log, action, terms) -> str | None:
    for row in log.execute("""SELECT DISTINCT a.id FROM actions a JOIN messages m ON a.id=m.action_id
        WHERE a.id<>? AND m.channel='email' AND m.status IN ('sent','delivered','sending','delivery_unknown')
        """, (action["id"],)):
        other = actions._get(log, row["id"])
        if other["payload"].get("repeat_commitment"):
            continue
        prior = other["payload"].get("commitment")
        prior_kind = prior.get("instruction", other["kind"]) if prior else instruction_kind(other["kind"], other["messages"])
        if not prior_kind:
            continue
        prior = prior or describe(other)
        if (prior.get("commitment_id") or prior.get("key")) == terms["commitment_id"]:
            return other["id"]
        if prior.get("part") != terms["part"] or prior.get("supplier_id") != terms["supplier_id"]:
            continue
        same_instruction = prior_kind == terms["instruction"] or {prior_kind, terms["instruction"]} <= {"order", "dispatch"}
        if not same_instruction:
            continue
        if terms_for(other):
            if terms["po"] and terms["po"] == prior["po"] and terms["po_line"] == prior["po_line"]:
                return other["id"]
        elif not terms["po"] or terms["po"] in _values(prior.get("po")):
            return other["id"]
    return None


def _authorized_repeat(log, action, terms):
    origin = actions._get(log, action["payload"]["repeat_commitment"])
    reference = terms_for(origin) if origin else None
    if not reference or origin["payload"].get("repeat_commitment") or reference != terms:
        return False
    subject, body = render_instruction(reference)
    return (origin["status"] in {"sent", "done"} and _signed(origin, fingerprint(reference)) and
            any(m["status"] == "sent" and m["channel"] == "email"
                and m["body"] == body and m["subject"] == subject
                and (m["to_address"] or "").casefold() == terms["recipient"] for m in origin["messages"]))


def check(action: dict) -> tuple[bool, str | None]:
    """Verify the exact approved instruction and current evidence before each send."""
    reason = proposal_blocker(action)
    if reason:
        return False, reason
    try:
        terms = terms_for(action)
        with actions.tx() as log:
            if action["payload"].get("repeat_commitment"):
                if action["status"] not in {"auto", "sent"} or not _authorized_repeat(log, action, terms):
                    return False, "repeat needs the exact terms of an explicitly approved and sent commitment"
            else:
                if other := _duplicate(log, action, terms):
                    return False, f"duplicate of {other}; the commitment already went out or delivery is unconfirmed"
                if action["status"] not in {"approved", "sent"} or not _signed(action, fingerprint(terms)):
                    return False, "verified approval for these exact terms is missing"
            if not _fresh(log):
                return False, "evidence stale"
            if reason := _related_replies(log, action, terms):
                return False, reason
        reason = _file_blocker(action, terms)
        return reason is None, reason
    except (sqlite3.Error, ValueError, TypeError):
        return False, "evidence stale or unreadable"


def decision_state(action: dict) -> dict:
    held = action["held_reason"]
    blocker = proposal_blocker(action) if applies(action) and not action["payload"].get("repeat_commitment") else None
    expired = action["status"] == "expired" or bool(blocker and "expired" in blocker) or bool(held and "expired" in held)
    replaced = re.search(r"duplicate of (ACT-[A-Z0-9]+)", held or "")
    if action["status"] == "superseded":
        terminal = f"Replaced by {replaced[1]}" if replaced else "Replaced"
    elif expired:
        terminal = "Expired — no longer useful"
    else:
        terminal = None
    reason = held or blocker
    if reason in {"evidence stale", "evidence stale or unreadable"}:
        owner, clears = "Dockside operator", "Restore portal sync and mailbox polling, then refresh both sources."
    elif reason and reason.startswith("required condition missing"):
        condition = reason.partition(": ")[2]
        owner = "Carrier" if condition == "transport confirmed" else "Receiving team"
        terms = terms_for(action) or {}
        clears = f"Confirm {condition.removesuffix(' confirmed')} for delivery {terms.get('delivery_id')} in the linked email thread."
    elif reason and "supplier reply" in reason:
        owner, clears = "Buyer", "Review the latest supplier reply, then submit the instruction with the current terms."
    elif reason and reason.startswith("verified approval"):
        owner, clears = action["approver_name"] or "Buyer", "Submit a new decision so every assigned approver can sign the exact terms."
    else:
        owner = action.get("owner") or "Buyer"
        clears = "Review the current evidence and submit a corrected shipment instruction for approval."
    return {"terminal": terminal, "can_approve": not (terminal or blocker) and action["status"] in
            {"proposed", "scheduled", "escalated"}, "held_reason": reason,
            "hold_owner": owner if reason and not terminal else None,
            "hold_clears": clears if reason and not terminal else None}
