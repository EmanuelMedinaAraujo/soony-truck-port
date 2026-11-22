"""Typed supplier instructions and release checks against current evidence."""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import unicodedata
from contextlib import closing
from decimal import Decimal
from email.utils import parseaddr
from fnmatch import fnmatchcase
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
WEEKDAY = r"Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?"
DAY = rf"\d{{4}}-\d{{2}}-\d{{2}}|{WEEKDAY}"
DAY_SEPARATOR = r"(?:T|\s+(?:(?:at|from)\s+)?|,\s*)"
CLOCK = r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?"
ZONE = r"Z|UTC|CEST|CET|[+-](?:0\d|1[0-4]):[0-5]\d"
TIME_ZONE = rf"(?:[ \t]*({ZONE}))?(?![\w:+-]|\s+(?:(?-i:[A-Z]{{2,5}})\b|[ap]\.?m\.?\b))"
ARRIVAL = rf"(?:({DAY}){DAY_SEPARATOR})?({CLOCK}){TIME_ZONE}"
SLOT_RANGE = rf"(?:({DAY}){DAY_SEPARATOR})?({CLOCK})\s*(?:[–—-]|\bto\b)\s*({CLOCK}){TIME_ZONE}"
ETA = re.compile(rf"\b(?:ETA|estimated (?:time of )?arrival|arrival|Ankunft|przyjazd)\s*"
                 rf"(?:(?:is|on|at)\s+|[:=,]\s*)*{ARRIVAL}", re.I)
WINDOW = re.compile(rf"\b(?:slot|window|Zeitfenster|okno)\b\s*"
                    rf"(?:(?:is|has been|confirmed|booked|window|for|on|at|from)\s*|[:=,.]\s*)*"
                    rf"{SLOT_RANGE}", re.I)
RAW_ETA = re.compile(rf"(?<![\w:+-]){ARRIVAL}", re.I)
RAW_WINDOW = re.compile(rf"(?<![\w:+-]){SLOT_RANGE}", re.I)
DEPARTURE = re.compile(
    rf"\b(?:departed|left|actual departure|abgefahren|wyjechał[ao]?)\s*"
    rf"(?:[^\d.!?;\n,:]+?\s+(?:at|um|o)\s+|(?:(?:on|at|um|o)\s+|[:=,]\s*)*){ARRIVAL}", re.I)
PROJECTED_DEPARTURE = re.compile(r"\b(?:plan\w*|expect\w*|schedul\w*|will|would|should|must|may|might|"
                                r"estimated|forecast|ETD|soll|voraussichtlich|prawdopodobnie)\b", re.I)
DEPARTURE_HOLD = "departure deadline passed"
BOOKING = re.compile(r"\b(?:booking|Buchungsnummer|numer rezerwacji)"
                     r"(?:\s+(?:ref(?:erence)?\.?|number|no\.?|ID|code))?\s*[:=#]?\s*"
                     r"([A-Z0-9][A-Z0-9._/-]{2,79})(?![\w/-])", re.I)
NEGATIVE_CONFIRMATION = re.compile(
    r"\b(?:cancel\w*|unavailable|cannot|can't|not|pending|tentative|provisional|if|subject to|awaiting|"
    r"storniert|nicht|vorläufig|odwołan\w*|nie|wstępn\w*)\b|[?]", re.I)
AMBIGUOUS_DATE = re.compile(r"\b(?:today|tonight|tomorrow|yesterday|next week)\b", re.I)
INSTRUCTIONS = {"order": "Purchase and dispatch", "cancel_order": "Cancel an existing order",
                "dispatch": "Dispatch", "expedite": "Expedite dispatch",
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
MONTHS = {name.casefold(): month for month, name in enumerate(calendar.month_name) if name}
MONTHS.update({name: month for month, name in enumerate(
    "stycznia lutego marca kwietnia maja czerwca lipca sierpnia września października listopada grudnia".split(), 1)})
HISTORICAL_DEADLINE = re.compile(
    rf"\b(?:(\d{{4}}-\d{{2}}-\d{{2}})|(\d{{1,2}})\s+({'|'.join(MONTHS)})(?:\s+(\d{{4}}))?)\b"
    rf"[^.!?;\n]*?(?:no later than|najpóźniej do(?: godziny)?)\s+({CLOCK}){TIME_ZONE}", re.I)
VEHICLE_LIMIT = re.compile(
    r"(?:maximum permitted gross weight of|dopuszczalnej masie całkowitej maksymalnie)"
    r"\s*(\d+(?:[.,]\d+)?)\s*(?:t|tonnes?)\b", re.I)
VEHICLE_RATING = re.compile(
    r"\b(?:rated(?: at| for)?|(?:maximum |permitted )?gross(?: vehicle)? weight|GVW|MAM|DMC)"
    r"(?:\s+(?:of|is))?\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*(?:t|tonnes?)\b", re.I)


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
    if terms["instruction"] == "cancel_order":
        ref = terms["order_reference"]
        text = f"Cancel the existing order {ref}. Do not dispatch it. Please confirm receipt of this cancellation."
        translation = {
            "pl": f"Prosimy anulować zamówienie {ref} i nie wysyłać towaru. Prosimy o potwierdzenie otrzymania anulowania.",
            "de": f"Bitte stornieren Sie die Bestellung {ref} und versenden Sie die Ware nicht. Bitte bestätigen Sie den Erhalt der Stornierung.",
        }.get(terms["language"])
        return f"Cancel order {ref}", "\n\n".join(filter(None, (translation, text, terms_block(terms))))
    verb = INSTRUCTIONS[terms["instruction"]]
    key = terms["commitment_id"]
    subject = f"{'Repeat · ' if repeat else ''}{verb} · {key}"
    opening = (f"Repeat of {key}. Carry out the existing instruction below. Do not create an additional order or booking."
               if repeat else f"{verb} according to these terms.")
    if original := terms.get("revises_action_id"):
        subject = f"Amend delivery terms only · {original}"
        opening = (f"Amend delivery terms for existing instruction {original}. "
                   "Keep the existing quantity and prices. Do not create another order or booking.")
        amendment = {
            "de": f"Nur Lieferbedingungen der bestehenden Bestellung {original} ändern. Keine zusätzliche Bestellung oder Buchung anlegen.",
            "pl": f"Zmiana wyłącznie warunków dostawy dla istniejącego zamówienia {original}. Nie tworzyć dodatkowego zamówienia ani rezerwacji.",
        }.get(terms["language"])
        return (f"{amendment} · {subject}" if amendment else subject,
                "\n".join(line for line in (amendment, opening, "", terms_block(terms)) if line is not None))
    if local := LOCAL_INSTRUCTIONS.get(terms["language"]):
        verb_local = local[terms["instruction"]]
        subject = f"{verb_local} · {subject}"
        opening = f"{LOCAL_REPEAT[terms['language']] if repeat else verb_local + '.'}\n{opening}"
    return subject, f"{opening}\n\n{terms_block(terms)}"


def setting(key, default):
    try:
        value = rules.cfg(key)
    except KeyError:
        return default
    if key == "staff_accounts":
        return {address: name for account, name in value.items()
                if (address := demo_staff_address(name) if account.startswith("+") else account)}
    return value


def demo_staff_address(name):
    if not setting("demo_approval_aliases", False):
        return None
    _, sender = parseaddr(os.environ.get("MAIL_FROM", ""))
    local, _, domain = sender.rpartition("@")
    tag = ".".join(re.findall(r"[a-z0-9]+", name.casefold()))
    if local and tag and domain.casefold() in {"gmail.com", "googlemail.com"}:
        return f"{local.split('+', 1)[0]}+{tag}@{domain}"
    return None


def init_schema(log) -> None:
    log.execute(CHECKPOINT)


def record_check(source: str, *, valid: bool = True) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).isoformat() if valid else None
    with actions.tx() as log:
        log.execute("INSERT OR REPLACE INTO release_evidence VALUES (?,?)", (source, stamp))


def _fresh(log) -> bool:
    stamps = dict(log.execute("SELECT source, checked_at FROM release_evidence"))
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(minutes=setting("release_max_age_minutes", 5))
    return all(stamps.get(source) and cutoff <= rules.ts(stamps[source]) <= now for source in ("files", "mailbox"))


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


def _action_log():
    return actions.read()


def _offers(db, supplier, part):
    return [offer for offer in rules.facts(db, "offer")
            if offer.get("supplier_id") == supplier and offer.get("material_id") == part]


def _offer_price(db, offer):
    from core.dispatch import offer_price
    return offer_price(db, offer)[0]


def _latest_offer_release(offer: dict) -> str | None:
    from core.dispatch import hours_in
    load, drive = (hours_in(offer.get(field)) for field in ("lead_time_note", "transport"))
    if load is None or drive is None:
        return None
    return (_receiving_cutoff() - dt.timedelta(hours=load + drive)).isoformat()


def _receiving_cutoff():
    return rules.ts(rules.cfg("deadline")) - dt.timedelta(hours=rules.cfg("receiving_buffer_hours"))


def _derive_depart_by(terms, arrival):
    from core.dispatch import hours_in

    if not arrival or "T" not in arrival:
        return None
    drive = setting("release_drive_hours", {}).get(terms["delivery_id"])
    if drive is None and rules.DB_PATH.is_file():
        with _master() as db:
            offers = _offers(db, terms["supplier_id"], terms["part"])
        durations = {hours_in(offer.get("transport")) for offer in offers}
        if len(durations) == 1:
            drive = durations.pop()
    if type(drive) in (int, float) and math.isfinite(drive) and drive > 0:
        return (rules.ts(arrival) - dt.timedelta(hours=drive)).isoformat()
    return None


def _prior_terms(log, original_id):
    original = actions._get(log, original_id)
    return delivery_terms(original) if original and sent_order_message(original) else None


def _revision_depart_by(original):
    return original.get("depart_by") or _derive_depart_by(original, _receiving_cutoff().isoformat())


def describe(action: dict, *, parent: dict | None = None) -> dict:
    """Capture an instruction from provided terms and uniquely identifiable source data."""
    payload = action["payload"]
    saved = payload.get("commitment") or {}
    if action["kind"] == "cancel_order":
        return saved
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
                if instruction == "order" and (not part or not supplier):
                    matches = {(offer["supplier_id"], offer["material_id"]) for offer in rules.facts(db, "offer")
                               if finding == f"offer-{offer.get('supplier_id')}-{offer.get('material_id')}"}
                    if len(matches) == 1:
                        offer_supplier, offer_part = matches.pop()
                        supplier, part = supplier or offer_supplier, part or offer_part
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
                    if not saved.get("revises_action_id"):
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
    terms = {"version": TERMS_VERSION, "commitment_id": key, "instruction": instruction, "delivery_id": delivery_id,
            "supplier_id": supplier, "recipient": addresses[0] if len(addresses) == 1 else None,
            "language": language if language in LOCAL_INSTRUCTIONS else "en",
            "part": part, "quantity": quantity, "unit_price": price, "total_price": total,
            "currency": currency, "incoterm": incoterm, "delivery_place": place, "po": po, "po_line": po_line,
            "emergency_reference": emergency, "deliver_by": deliver_by, "valid_until": valid,
            "latest_useful_at": None if saved.get("revises_action_id") else latest, "conditions": sorted(conditions)}
    if saved.get("vehicle_limit_t") is not None:
        terms["vehicle_limit_t"] = saved["vehicle_limit_t"]
    if saved.get("revises_action_id") is not None:
        terms["revises_action_id"] = saved["revises_action_id"]
    try:
        if original_id := terms.get("revises_action_id"):
            if parent and parent["id"] == original_id and sent_order_message(parent):
                original = delivery_terms(parent)
            else:
                with _action_log() as log:
                    original = _prior_terms(log, original_id)
            terms["depart_by"] = _revision_depart_by(original) if original else None
        else:
            terms["depart_by"] = _derive_depart_by(terms, deliver_by)
    except (ArithmeticError, sqlite3.Error, ValueError, TypeError):
        terms["depart_by"] = None
    return terms


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
    if terms["instruction"] == "cancel_order":
        subject, body = render_instruction(terms)
        if message["subject"] != subject or message["body"] != body or message.get("body_en"):
            return "cancellation draft differs from the approved instruction"
        return None
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


def cancellation_terms(order, task):
    sent = sent_order_message(order)
    terms = delivery_terms(order)
    reference = terms.get("po") or terms.get("emergency_reference") or order["id"]
    if terms.get("po") and terms.get("po_line"):
        reference += f", line {terms['po_line']}"
    key = "COM-" + hashlib.sha256(f"cancel:{order['id']}".encode()).hexdigest()[:20].upper()
    return {"version": TERMS_VERSION, "commitment_id": key, "instruction": "cancel_order",
            "recipient": sent["to_address"].casefold(), "language": (sent.get("language") or "en").split("-")[0].lower(),
            "order_reference": reference, "order_action_id": order["id"], "task_id": task["id"],
            "answer_reply_id": task["answered_reply_id"], "delivery_id": terms["delivery_id"], "total_price": 0}


def _cancellation_blocker(action, terms):
    messages = [m for m in action["messages"] if m["channel"] != "approval_email"]
    if (action["kind"] != "cancel_order" or action["cost_eur"] != 0 or len(messages) != 1
            or messages[0]["channel"] != "email" or not terms["recipient"]
            or parseaddr(terms["recipient"])[1] != terms["recipient"] or "@" not in terms["recipient"]
            or (messages[0]["to_address"] or "").casefold() != terms["recipient"]
            or action["payload"].get("commitment_digest") != fingerprint(terms)):
        return "cancellation needs a signed instruction for the original order and recipient"
    return _body_blocker(terms, messages[0])


def _cancellation_authorization(log, terms):
    from core import questions
    task = log.execute("SELECT * FROM staff_tasks WHERE id=? AND status='answered'", (terms["task_id"],)).fetchone()
    order = actions._get(log, terms["order_action_id"])
    if (not task or task["answer_text"] != questions.CANCEL_ORDER or task["action_id"] != terms["order_action_id"]
            or not order or not sent_order_message(order) or not delivery_terms(order)
            or cancellation_terms(order, task) != terms or not delivery_state(order, log=log)["conflict"]):
        return "the original order or recorded cancellation outcome changed"
    return None


def proposal_blocker(action: dict, *, check_departure=True) -> str | None:
    terms = terms_for(action)
    if not terms:
        return "typed commitment is missing"
    try:
        if terms["instruction"] == "cancel_order":
            return _cancellation_blocker(action, terms)
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
        limit = terms.get("vehicle_limit_t")
        if limit is not None and (type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0
                                  or "transport_confirmed" not in terms["conditions"]):
            return "a positive gross vehicle weight limit requires carrier confirmation"
        if "revises_action_id" in terms and (terms["instruction"] != "order"
                or not re.fullmatch(r"ACT-[A-Z0-9]+", terms["revises_action_id"])):
            return "a delivery amendment needs the original order action ID"
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
        if check_departure:
            return _departure_blocker(action, terms)
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


def _confirmation_contacts(terms):
    carriers = _values(setting("release_carrier_contacts", {}).get(terms["delivery_id"]))
    carriers += _values(setting("release_supplier_logistics_contacts", {}).get(terms["supplier_id"]))
    dock = {}
    if (terms.get("delivery_place") or "").strip().casefold() in _site_names():
        people, emails = setting("people", {}), setting("staff_email", {})
        for role in (setting("site_lead_role", "Site Lead"), setting("site_deputy_role", "Logistics Planner (deputy dock slots)")):
            name = people.get(role)
            if name and (address := emails.get(name) or demo_staff_address(name)):
                dock[address.casefold()] = name
    return {"transport_confirmed": {address.casefold(): address for address in carriers}, "dock_slot_confirmed": dock}


def _site_names():
    return {name.casefold() for name in (rules.setting("site"), setting("site_name", ""), setting("site_port", ""),
                                        *setting("site_aliases", [])) if name}


def _single_match(pattern, body, summary, *, raw_pattern=None, accept=lambda _groups: True):
    """Use a translation to locate literals found in the original reply."""
    matches = {match.groups() for match in pattern.finditer(body) if accept(match.groups())}
    if not matches:
        def normalize(groups):
            return tuple(part.casefold() if part else None for part in groups)

        raw = {normalize(match.groups()) for match in raw_pattern.finditer(body)} if raw_pattern else None
        matches = {match.groups() for match in pattern.finditer(summary)
                   if accept(match.groups()) and (
                       normalize(match.groups()) in raw if raw is not None else
                       re.search(rf"(?<![\w/-]){re.escape(match[1].rstrip('.'))}(?![\w/-])", body, re.I))}
    return matches.pop() if len(matches) == 1 else None


def _appointment(day, clock, zone, anchor):
    if not day:
        date = anchor.date()
    elif day[0].isdigit():
        date = dt.date.fromisoformat(day)
    else:
        weekday = ("mon", "tue", "wed", "thu", "fri", "sat", "sun").index(day[:3].lower())
        date = anchor.date() - dt.timedelta(days=(anchor.weekday() - weekday) % 7)
    offset = {"Z": "+00:00", "UTC": "+00:00", "CET": "+01:00", "CEST": "+02:00"}.get((zone or "").upper(), zone)
    tz = dt.timezone(dt.time.fromisoformat("00:00" + offset).utcoffset()) if offset else anchor.tzinfo
    return dt.datetime.combine(date, dt.time(*map(int, clock.split(":")), tzinfo=tz)).astimezone(anchor.tzinfo)


def _affirmed(condition, text):
    _, pattern = CONDITIONS[condition]
    if condition == "transport_confirmed":
        pattern += r"|(?:carrier )?booking (?:is |has been )?confirmed|confirmed (?:carrier )?booking"
    else:
        pattern += r"|confirmed (?:dock|unload|receiving) slot|slot\b[^.!?\n]*\bconfirmed"
    return re.search(rf"\b(?:{pattern})\b", text, re.I)


def _reported_departure(body, received):
    actual = "\n".join(line for line in re.split(r"(?<=[.;])\s+|\n+", body)
                       if not PROJECTED_DEPARTURE.search(line))
    if departed := _single_match(DEPARTURE, actual, ""):
        departure = _appointment(*departed, received)
        if departure <= received:
            return departure.isoformat()
    return None


def _confirmation_details(condition, reply, contact, deadline, eta, created, vehicle_limit, now, *, prior=None):
    details = {"reply_id": reply["id"], "contact": contact, "address": reply["from_addr"],
               "received_at": reply["received_at"]}
    if not reply["handled"]:
        return details, "reply needs review"
    body = _normalized(inbox._strip_quote(reply["body"] or ""))
    summary = _normalized(reply["summary_en"])
    try:
        if AMBIGUOUS_DATE.search(body):
            return details, "use a calendar date or weekday for the ETA or slot window"
        prior = prior or {}
        departure = None
        if condition == "transport_confirmed":
            received = rules.ts(reply["received_at"]).astimezone(deadline.tzinfo)
            departure = _reported_departure(body, received)
        departure_update = departure and prior.get("booking_reference")
        affirmative = (_affirmed(condition, body) or departure_update
                       or (_affirmed(condition, summary) and not NEGATIVE_CONFIRMATION.search(summary)))
        if NEGATIVE_CONFIRMATION.search(body) or not affirmative:
            return details, "reply needs an affirmative confirmation"
        if condition == "transport_confirmed":
            booking = _single_match(BOOKING, body, summary,
                                    accept=lambda groups: groups[0].isupper() or any(c.isdigit() for c in groups[0]))
            arrived = _single_match(ETA, body, summary, raw_pattern=RAW_ETA)
            missing = []
            if booking:
                details["booking_reference"] = booking[0].rstrip(".")
            elif departure_update and not BOOKING.search(body):
                details["booking_reference"] = prior["booking_reference"]
            else:
                missing.append("booking reference missing")
            same_booking = details.get("booking_reference") and details["booking_reference"] == prior.get("booking_reference")
            if departure or (same_booking and (departure := prior.get("departed_at"))):
                details["departed_at"] = departure
            if arrived:
                arrival = _appointment(*arrived, deadline)
                details["eta"] = arrival.isoformat()
                details["eta_expired"] = arrival < max(created, now)
                if details["eta_expired"] or arrival > deadline:
                    missing.append("ETA is outside the signed delivery deadline or has passed")
            else:
                missing.append("ETA missing or ambiguous")
            if vehicle_limit is not None:
                ratings = {float(m[1].replace(",", ".")) for m in VEHICLE_RATING.finditer(body)}
                if not ratings and departure_update and same_booking and prior.get("vehicle_rating_t"):
                    ratings.add(prior["vehicle_rating_t"])
                if not ratings or any(not 0 < weight <= vehicle_limit for weight in ratings):
                    missing.append(f"confirm vehicle gross weight rating at most {vehicle_limit:g} t")
                else:
                    details["vehicle_rating_t"] = max(ratings)
            return details, ", ".join(missing) or None

        if not (window := _single_match(WINDOW, body, summary, raw_pattern=RAW_WINDOW)):
            return details, "slot window missing or ambiguous"
        day, opening, closing, zone = window
        anchor = eta or deadline
        start = _appointment(day, opening, zone, anchor)
        end = _appointment(day, closing, zone, anchor)
        details.update(slot_start=start.isoformat(), slot_end=end.isoformat())
        if end <= start:
            return details, "slot window must end after it starts"
        if eta is None:
            return details, "slot window is awaiting a confirmed carrier ETA"
        if not start <= eta <= end:
            return details, "slot window does not cover the confirmed carrier ETA"
    except ValueError:
        return details, "reply contains an invalid delivery date or time"
    return details, None


def _related_replies(log, action, terms, now, *, after_departure=False):
    delivery_id = terms["delivery_id"]
    signed_conditions = set(terms["conditions"])
    conditions = set(CONDITIONS) if after_departure else (
        signed_conditions | {"transport_confirmed"} if "dock_slot_confirmed" in signed_conditions else signed_conditions)
    contacts = _confirmation_contacts(terms) if conditions else {}
    eligible, unauthorized, transport_threads = {}, set(), {}
    review = None
    created = rules.ts(action.get("created_at")) or now
    has_replies = "simulated" in {column[1] for column in log.execute("PRAGMA table_info(replies)")}
    replies = log.execute("SELECT * FROM replies WHERE simulated=0 ORDER BY julianday(received_at), rowid") if has_replies else ()
    for reply in replies:
        received = rules.ts(reply["received_at"])
        if not received or received > now:
            continue
        sender = (reply["from_addr"] or "").casefold()
        condition = _linked_condition(log, reply, delivery_id, sender) if conditions else None
        authorized = condition in contacts and sender in contacts[condition]
        if condition in conditions:
            if authorized and (condition != "dock_slot_confirmed" or not terms.get("revises_action_id")
                               or received >= created or _revision_slot_request(log, reply, action)):
                eligible[condition] = (reply, contacts[condition][sender])
                if condition == "transport_confirmed":
                    transport_threads.setdefault(reply["msg_id"], []).append(eligible[condition])
            elif not authorized:
                unauthorized.add(condition)
        if sender != terms["recipient"]:
            continue
        if not reply["handled"]:
            review = "an unread supplier reply needs review"
        elif not review and received >= created:
            body = _normalized(inbox._strip_quote(reply["body"] or ""))
            logistics_only = (authorized and condition == "transport_confirmed"
                              and (_affirmed(condition, body) or _reported_departure(body, received.astimezone(rules.timezone())))
                              and not _body_blocker(terms, {"body": body})
                              and not re.search(r"\b(?:PO|purchase order|price|quantity|qty|incoterm|DAP|EXW)\b",
                                                body, re.I))
            if not logistics_only:
                review = "newer supplier reply needs review of quantity, price and PO"

    matches = {}
    eta = None
    for condition in CONDITIONS:
        if condition not in conditions:
            continue
        if condition in eligible:
            reply, _ = eligible[condition]
            history = transport_threads[reply["msg_id"]] if condition == "transport_confirmed" else [eligible[condition]]
            deadline = rules.ts(terms["deliver_by"]).astimezone(rules.timezone())
            details = {}
            for observation in history:
                details, reason = _confirmation_details(condition, *observation, deadline, eta, created,
                                                        terms.get("vehicle_limit_t"), now, prior=details)
        else:
            details, reason = {}, ("reply sender is not authorized for this delivery" if condition in unauthorized
                                   else "no linked reply from an authorized contact")
        matches[condition] = {**details, "confirmed": reason is None, "reason": reason}
        if condition == "transport_confirmed" and details.get("eta") and not details.get("eta_expired"):
            eta = rules.ts(details["eta"])
    if set(setting("release_conditions", {}).get(terms["instruction"], [])) - signed_conditions:
        review = "required release conditions changed since approval"
    return review, matches


def _revision_slot_request(log, reply, action):
    origin = actions._get(log, reply["action_id"])
    return bool(origin and origin["kind"] == "confirm_terms" and origin["finding_id"] == action["finding_id"]
                and origin["finding_id"].startswith("followup-"))


def _linked_condition(log, reply, delivery_id, sender):
    origin = actions._get(log, reply["action_id"]) if reply["action_id"] else None
    if not origin:
        return None
    origin_terms = terms_for(origin)
    reference = delivery_reference(delivery_id)
    for message in origin["messages"]:
        if (message["id"] != reply["msg_id"] or message["channel"] != "email"
                or message["status"] not in {"sent", "delivered"}
                or (message["to_address"] or "").casefold() != sender):
            continue
        if origin_terms:
            linked = origin_terms["delivery_id"] == delivery_id
        else:
            linked = (reference and origin["payload"].get("delivery_id") == delivery_id
                      and message["body"].endswith("\n\n" + reference))
        if linked:
            kind = (origin_terms or {}).get("instruction") or origin["kind"]
            if kind == "confirm_terms":
                kind = origin["payload"].get("confirmation_kind")
            return next((condition for condition, (action_kind, _) in CONDITIONS.items() if kind == action_kind), None)
    return None


def confirmation_summary(matches):
    lines = []
    for condition, evidence in matches.items():
        if condition == "transport_confirmed" and evidence["confirmed"]:
            eta = rules.ts(evidence["eta"])
            lines.append(f"Carrier confirmed booking {evidence['booking_reference']}, ETA {eta:%a %H:%M}")
            if departed := rules.ts(evidence.get("departed_at")):
                lines.append(f"Departure at {departed:%a %H:%M} confirmed by {evidence['contact']}")
            elif position := evidence.get("departure_position"):
                observed = rules.ts(position["observed_at"])
                lines.append(f"Carrier position at {observed:%a %H:%M} confirms departure")
        elif condition == "dock_slot_confirmed" and evidence["confirmed"]:
            start, end = (rules.ts(evidence[key]) for key in ("slot_start", "slot_end"))
            lines.append(f"Slot {start:%H:%M}–{end:%H:%M} confirmed by {evidence['contact']}")
        else:
            label = "Carrier booking and ETA" if condition == "transport_confirmed" else "Dock slot"
            lines.append(f"{label}: {evidence['reason']}")
    return lines


def sent_order_message(action):
    return next((message for message in action["messages"]
                 if message["channel"] == "email" and message["status"] in {"sent", "delivered"}
                 and message.get("sent_at")), None)


def _historical_terms(action):
    message = sent_order_message(action) if action["kind"] == "order" else None
    if not message:
        return None
    sent = rules.ts(message["sent_at"]).astimezone(rules.timezone())
    body = "\n".join(_normalized(message.get(field)) for field in ("body", "body_en"))
    try:
        dates = set()
        for iso_date, day, month, year, clock, zone in HISTORICAL_DEADLINE.findall(body):
            if not iso_date:
                date = dt.date(int(year or sent.year), MONTHS[month.casefold()], int(day))
                if not year and date < sent.date():
                    date = date.replace(year=sent.year + 1)
                iso_date = date.isoformat()
            dates.add(_appointment(iso_date, clock, zone or None, sent))
    except ValueError:
        return None
    if len(dates) != 1:
        return None
    deadline = dates.pop()
    if deadline < sent:
        return None
    limits = {float(match[1].replace(",", ".")) for match in VEHICLE_LIMIT.finditer(body)}
    if len(limits) > 1:
        return None
    terms = describe(action)
    qty, cost, currency = _historical_amounts(terms["part"], body)
    sites = "|".join(re.escape(name) for name in sorted(_site_names(), key=lambda name: (-len(name), name)))
    incoterms = {match[1].upper() for match in re.finditer(
        rf"\b({'|'.join(sorted(INCOTERMS))})\s+(?:DC\s+)?(?:{sites})(?![\w-])", body, re.I)} if sites else set()
    incoterm = incoterms.pop() if len(incoterms) == 1 else None
    terms.update(delivery_id=action["id"], emergency_reference=action["id"], deliver_by=deadline.isoformat(),
                 vehicle_limit_t=next(iter(limits), None), quantity=qty, total_price=cost,
                 unit_price=round(cost / qty, 8) if cost is not None and qty else None, currency=currency,
                 incoterm=incoterm, delivery_place=setting("site_name", None) if incoterm else None,
                 conditions=sorted(set(terms["conditions"]) | CONDITIONS.keys()))
    terms["depart_by"] = _derive_depart_by(terms, _receiving_cutoff().isoformat())
    return terms


def _historical_amounts(part, body):
    from core.ingest import num

    names = [part]
    label = setting("display", {}).get("parts", {}).get(part)
    if label:
        names.append(label[1])
    if part and rules.DB_PATH.is_file():
        with _master() as db:
            names.extend(row[0] for row in db.execute("SELECT description FROM components WHERE component_id=?", (part,)))
    labels = "|".join(re.escape(_normalized(name)) for name in names if name)
    part_quantity = re.compile(rf"(?<![\w.,-])({NUMBER})\s+(?:finished\s+)?(?:{labels})s?(?![\w-])", re.I) if labels else None
    matches = list(part_quantity.finditer(body)) if part_quantity else []
    if not matches:
        matches = list(QUANTITY_LABEL.finditer(body))
    quantities = {num(match[1]) for match in matches}
    qty = quantities.pop() if len(quantities) == 1 else None
    totals = {(num(match[2] or match[3]), (match[1] or match[4]).upper()) for match in re.finditer(
        rf"\btotal(?:\s+price)?\s*(?:of\s+|[:=]\s*)?{PRICE.pattern}", body, re.I)
              if re.fullmatch(r"[A-Za-z]{3}", match[1] or match[4])}
    cost, currency = totals.pop() if len(totals) == 1 else (None, None)
    return qty, cost, currency


def delivery_terms(action):
    """Use typed terms when available, otherwise the evidence in the sent order."""
    return terms_for(action) or _historical_terms(action)


ACCEPTED_ORDER = re.compile(
    r"\b(?:accept\w*\b.{0,65}\border|\btreat\w*\b.{0,120}\bas\s+(?:the|a)\b.{0,60}\border"
    r"|releas\w* the goods|traktuję.{0,80}jako pisemne zlecenie)", re.I)
DISPUTED_ORDER = re.compile(
    r"\b(?:no\s+(?:written\s+)?(?:(?:dispatch|shipping) (?:instruction|release)|booking in (?:our|the|its) (?:current )?records)"
    r"|(?:order|goods|shipment|dispatch)\b.{0,30}\b(?:not (?:been )?(?:released|accepted|received)|was never released)"
    r"|(?:not|never|cannot|can't|won't)\s+(?:been\s+)?(?:released|accept(?:ed)? the order|received.{0,30}dispatch)"
    r"|no\b.{0,65}\bdispatch release|nie otrzymali\w*\s+(?:również\s+)?dyspozycji"
    r"|(?:wrong|incorrect|different|revised)\s+(?:PO\b|purchase order|quantity|price)"
    r"|(?:PO\b|purchase order|quantity|price)\s+(?:is\s+)?(?:incorrect|wrong|does not match))", re.I)
ACKNOWLEDGED_ORDER = re.compile(r"\b(?:acknowledg\w*|confirm\w* receipt|potwierdzam otrzymanie)\b", re.I)
CLAIMED_PO = re.compile(r"\b(?:correct|new|actual)\s+PO(?:\s+(?:number|reference))?\s*[:#]?\s*(\d{6,})\b", re.I)
NOTE_START = re.compile(
    r"(?:[;,]|(?<=[.!?]))\s*(?=(?:and\s+|so\s+|(?:so |and )?then\s+)?"
    r"(?:(?:Dockside|ACT-[A-Z0-9]+)\b|(?:I|we)\s+(?:resent|sent|asked|placed|recorded|chased|updated|logged|escalated)\b"
    r"|using\b.{0,130}\bmodel(?:ed|led)\b|(?:the\s+)?(?:earliest\s+)?model(?:ed|led)\b|so no\b))", re.I)


def _claim_rows(reply, terms):
    body = inbox._strip_quote(reply["body"] or "")
    summary = " ".join((reply["summary_en"] or "").split())
    words, *notes = NOTE_START.split(summary)
    notes = [note.strip(" ,;") for note in notes]
    words = words.rstrip(" ,;")
    if re.match(r"^(?:Dockside|ACT-[A-Z0-9]+)\b", words, re.I):
        notes.insert(0, words)
        words = ""
    if not (words or body):
        return []
    content = " ".join(f"{body} {words}".split())
    commercial_change = (all(terms.get(k) is not None for k in ("quantity", "unit_price", "total_price", "currency"))
                         and _body_blocker(terms, {"body": body, "body_en": words}))
    po_change = terms.get("po") and any(po != terms["po"] for po in CLAIMED_PO.findall(content))
    if DISPUTED_ORDER.search(content) or commercial_change or po_change:
        kind = "disputed"
    elif ACCEPTED_ORDER.search(content):
        kind = "accepted"
    elif ACKNOWLEDGED_ORDER.search(content):
        kind = "acknowledged"
    else:
        kind = "reported"
    event = {"message_id": reply["id"], "at": reply["received_at"]}
    claim = event | {"who": reply["from_name"] or reply["contact"], "kind": kind, "text": words or body}
    trace = [claim]
    for note in filter(None, notes):
        kind = _dockside_event_kind(note)
        previous = trace[-1]
        if kind == "estimate" and previous["who"] == "Dockside" and previous["text"].lower().startswith("using "):
            previous.update(kind=kind, text=f"{previous['text']}, {note}")
        else:
            trace.append(event | {"who": "Dockside", "kind": kind, "text": note})
    return trace


def _dockside_event_kind(text):
    # Attribute an action even when the action description mentions a calculation.
    if re.match(r"^(?:so |and )?(?:Dockside|ACT-[A-Z0-9]+|I|we)\s+"
                r"(?:escalated|recorded|sent|asked|placed|chased|logged|updated|resent)\b", text, re.I):
        return "note"
    return "estimate" if re.search(r"\b(?:model(?:ed|led)|estimat(?:e|ed)|calculat(?:ed|es))\b", text, re.I) else "note"



def supplier_claims(log, action, terms, now):
    """Read the order thread and linked follow-ups from the supplier who received the order."""
    sent = sent_order_message(action)
    if not sent or "simulated" not in {column[1] for column in log.execute("PRAGMA table_info(replies)")}:
        return []
    replies = log.execute("""WITH RECURSIVE related(id) AS (
        SELECT id FROM actions
        WHERE id=? OR finding_id=? OR
            (substr(finding_id, 1, length('followup-' || ? || '-'))='followup-' || ? || '-')
        UNION
        SELECT a.id FROM related JOIN replies r ON r.action_id=related.id
        JOIN actions a ON a.finding_id='reply-' || r.id
    )
    SELECT r.*, m.to_name AS contact FROM related JOIN replies r ON r.action_id=related.id
    JOIN messages m ON m.id=r.msg_id AND m.action_id=r.action_id
    WHERE r.simulated=0 AND m.channel='email' AND m.status IN ('sent','delivered')
      AND lower(r.from_addr)=lower(?) AND lower(m.to_address)=lower(r.from_addr)
      AND julianday(r.received_at) BETWEEN julianday(?) AND julianday(?)
    ORDER BY julianday(r.received_at), r.rowid""",
                          (action["id"], action["finding_id"], action["finding_id"], action["finding_id"],
                           terms["recipient"], sent["sent_at"], now.isoformat()))
    return [claim for reply in replies for claim in _claim_rows(reply, terms)]


def _order_conflict(action, claims, transport, contacts, terms, now):
    material = [claim for claim in claims if claim["kind"] in {"disputed", "accepted"}]
    dispute = next((claim for claim in reversed(material) if claim["kind"] == "disputed"), None)
    if not dispute:
        return None
    # A later acceptance resolves the supplier's side of the conflict. Require fresh logistics evidence too.
    eta = rules.ts(transport.get("eta"))
    observation = rules.ts(transport.get("received_at"))
    cutoff = _receiving_cutoff()
    if (material[-1]["kind"] == "accepted" and observation and observation >= rules.ts(dispute["at"])
            and eta and now <= eta <= cutoff and transport.get("booking_reference")
            and (terms.get("vehicle_limit_t") is None or transport.get("vehicle_rating_t") is not None)):
        return None
    buyer = action.get("approver_name") or "Buyer"
    addresses = list(contacts.get("transport_confirmed", {}).values())
    logistics = ", ".join(addresses) or "the supplier logistics contact or carrier"
    # In the sentence people read, name the person: the supplier contact who wrote, or the carrier's logistics desk.
    own = str(terms.get("recipient") or "").rsplit("@", 1)[-1].casefold()
    spoken = ", ".join(dict.fromkeys(
        value if "@" not in str(value) else
        f"{dispute['who']} (supplier logistics)" if own and str(value).rsplit("@", 1)[-1].casefold() == own else
        "the carrier's logistics desk" for value in addresses)) or logistics
    return dispute | {
        "owner_reconcile": buyer,
        "owner_logistics": logistics,
        "clears_when": (f"{buyer} reconciles the order reference, quantity, price and dispatch release with the supplier. "
                        f"{spoken} verifies a booking, eligible vehicle and current ETA by the {cutoff:%H:%M} receiving cutoff. "
                        "Receiving must confirm an unload slot for that ETA before release."),
    }


def amendment_window(original: dict, cutoff: dt.datetime | None = None) -> dict | None:
    """The original deadline is exclusive and the receiving cutoff is inclusive."""
    if not (after := rules.ts(original.get("deliver_by"))):
        return None
    if cutoff is None:
        cutoff = _receiving_cutoff()
    return {"after": after.isoformat(), "latest": cutoff.isoformat()}


def verified_eta(delivery: dict) -> str | None:
    """A verified carrier observation can be too late for the original order."""
    transport = delivery.get("confirmation_evidence", {}).get("transport_confirmed", {})
    return delivery.get("eta") if not (delivery.get("conflict") or transport.get("eta_expired")) else None


def amendment_deadline_blocker(original: dict, deliver_by: str | None, eta: str | None = None) -> str | None:
    try:
        window = amendment_window(original)
        deadline = rules.ts(deliver_by)
        if not window or not deadline or not rules.ts(window["after"]) < deadline <= rules.ts(window["latest"]):
            return "Amended deliver_by must be inside arrival_window: after the original deadline and at or before the receiving cutoff."
        if eta and deadline < rules.ts(eta):
            return "Amended deliver_by must be at or after the verified carrier ETA."
    except (AttributeError, TypeError, ValueError):
        return "Amended deliver_by must be a valid ISO arrival time."
    return None


def _effective_depart_by(terms, log):
    # Preserve the digest of commitments signed before the departure field existed.
    if "depart_by" in terms:
        return rules.ts(terms["depart_by"])
    if original_id := terms.get("revises_action_id"):
        original = _prior_terms(log, original_id)
        return rules.ts(_revision_depart_by(original)) if original else None
    return rules.ts(_derive_depart_by(terms, terms.get("deliver_by")))


def _carrier_departure(terms, booking, depart_by, now):
    from core.dispatch import coordinates

    if not rules.DB_PATH.is_file():
        return None
    with _master() as db:
        rows = db.execute("""SELECT shipment_id, reference, material_ids, dest_site, last_lat, last_lon, updated_at,
                                    ports.lat, ports.lon
                             FROM shipments JOIN carriers USING (carrier_id)
                             LEFT JOIN ports ON ports.locode=shipments.origin
                             WHERE (shipment_id=? OR reference=?)
                               AND supplier_id=? AND shipments.mode='road'
                               AND status IN ('in_transit', 'in_delivery', 'delayed')""",
                          (terms["delivery_id"], booking, terms["supplier_id"])).fetchall()
    if len(rows) != 1:
        return None
    shipment_id, reference, materials, site, lat, lon, observed, origin_lat, origin_lon = rows[0]
    if (terms["part"] not in (materials or "").split("|") or (site or "").casefold() not in _site_names()
            or not coordinates(lat, lon) or (lat, lon) == (origin_lat, origin_lon) or not observed or "T" not in observed):
        return None
    observed_at = rules.ts(observed)
    freshness = dt.timedelta(hours=setting("staleness_hours", {}).get("road", 24))
    if now - freshness <= observed_at <= depart_by:
        return {"shipment_id": shipment_id, "reference": reference, "observed_at": observed_at.isoformat(),
                "lat": lat, "lon": lon}
    return None


def _departure_evidence(log, terms, matches, depart_by, now):
    transport = matches.get("transport_confirmed", {})
    slot = matches.get("dock_slot_confirmed", {})
    if not (transport.get("confirmed") and slot.get("confirmed")):
        return DEPARTURE_HOLD
    eta = rules.ts(transport["eta"])
    freshness = dt.timedelta(hours=setting("staleness_hours", {}).get("road", 24))
    if not (now <= eta and rules.ts(transport["received_at"]) >= now - freshness):
        return DEPARTURE_HOLD
    if original_id := terms.get("revises_action_id"):
        original = _prior_terms(log, original_id)
        if not original or eta <= rules.ts(original["deliver_by"]):
            return DEPARTURE_HOLD
    if (departed := rules.ts(transport.get("departed_at"))) and departed <= depart_by:
        return None
    if position := _carrier_departure(terms, transport["booking_reference"], depart_by, now):
        transport["departure_position"] = position
        return None
    return DEPARTURE_HOLD


def _departure_blocker(action, terms):
    """Read evidence without taking a write lock inside the approval transaction."""
    if "depart_by" in terms and not terms["depart_by"]:
        return "departure deadline is missing" if terms.get("revises_action_id") else None
    now = rules.now()
    if terms.get("depart_by") and now <= rules.ts(terms["depart_by"]) and not terms.get("revises_action_id"):
        return None
    if not actions.DB.is_file():
        return DEPARTURE_HOLD if terms.get("depart_by") else None
    try:
        with _action_log() as log:
            if original_id := terms.get("revises_action_id"):
                original = actions._get(log, original_id)
                if original and (conflict := delivery_state(original, log=log, now=now)["conflict"]):
                    return conflict_reason(conflict)
            depart_by = _effective_depart_by(terms, log)
            if depart_by is None:
                return "departure deadline is missing" if terms.get("revises_action_id") else None
            if now > depart_by:
                _, matches = _related_replies(log, action, terms, now, after_departure=True)
                return _departure_evidence(log, terms, matches, depart_by, now)
    except sqlite3.Error:
        return "evidence stale or unreadable"
    return None


def conflict_reason(conflict):
    at = rules.ts(conflict["at"]).astimezone(rules.timezone())
    text = conflict["text"].rstrip(".!?")
    return f"Supplier reply from {conflict['who']} at {at:%H:%M} disputes the order. {text}. {conflict['clears_when']}"


def delivery_state(action, *, depart_for_requested=None, now=None, log=None):
    """The gate and cockpit share the same reply evidence and delivery constraints."""
    now = now or rules.now()
    terms = delivery_terms(action)
    matches, review, departure_hold, claims = {}, None, None, []
    depart_by = (terms or {}).get("depart_by")
    if terms and terms.get("deliver_by"):
        def evidence(db):
            departure = _effective_depart_by(terms, db)
            after_departure = departure is not None and now > departure
            supplier_review, confirmations = _related_replies(db, action, terms, now, after_departure=after_departure)
            hold = _departure_evidence(db, terms, confirmations, departure, now) if after_departure else None
            if departure is None and terms.get("revises_action_id"):
                hold = "departure deadline is missing"
            return supplier_review, confirmations, hold, departure, supplier_claims(db, action, terms, now)

        try:
            if log is None:
                with _action_log() as db:
                    review, matches, departure_hold, depart_by, claims = evidence(db)
            else:
                review, matches, departure_hold, depart_by, claims = evidence(log)
        except (ArithmeticError, sqlite3.Error, ValueError, TypeError):
            review = "evidence stale or unreadable"
    terms = terms or {}
    transport = matches.get("transport_confirmed", {})
    dock = matches.get("dock_slot_confirmed", {})
    deadline = rules.ts(terms.get("deliver_by"))
    eta = rules.ts(transport.get("eta"))
    limit = terms.get("vehicle_limit_t")
    contacts = _confirmation_contacts(terms) if terms else {}
    conflict = _order_conflict(action, claims, transport, contacts, terms, now)
    revised_reason = None
    if conflict:
        revised_reason = conflict_reason(conflict)
    elif eta and deadline and eta > deadline:
        revised_reason = f"Verified ETA {eta:%a %H:%M} is after the ordered {deadline:%H:%M} arrival."
    elif deadline and (now >= deadline or (depart_for_requested and now > depart_for_requested)) and (not eta or eta < now):
        revised_reason = f"The ordered {deadline:%H:%M} arrival can no longer be met."
    hold_reason = revised_reason if conflict else departure_hold or review or _confirmation_blocker(matches)
    saved_hold = (action.get("payload") or {}).get("held_reason")
    if (log is None and not hold_reason and saved_hold and saved_hold != DEPARTURE_HOLD
            and not saved_hold.startswith("required condition missing")):
        hold_reason = saved_hold
    return {
        "action_id": action["id"], "delivery_id": terms.get("delivery_id"), "deliver_by": terms.get("deliver_by"),
        "depart_by": depart_by.isoformat() if isinstance(depart_by, dt.datetime) else depart_by,
        "revises_action_id": terms.get("revises_action_id"),
        "terms_source": ("signed_terms" if _signed(action, fingerprint(terms)) else "proposed_terms") if terms_for(action)
                        else "sent_order" if terms else None,
        "booking_ref": transport.get("booking_reference"), "eta": transport.get("eta"),
        "eta_source": transport.get("contact") if eta else None, "transport_confirmed": bool(transport.get("confirmed") and not conflict),
        "slot_window": {"start": dock["slot_start"], "end": dock["slot_end"]} if dock.get("slot_end") else None,
        "slot_confirmed": bool(dock.get("confirmed") and not conflict),
        "slot_confirmed_by": dock.get("contact") if dock.get("slot_end") else None,
        "vehicle_limit": limit, "vehicle_rating": transport.get("vehicle_rating_t"),
        "vehicle_ok": (transport.get("vehicle_rating_t") is not None) if limit is not None else None,
        "hold_reason": hold_reason,
        "needs_revised_terms": bool(revised_reason), "revised_reason": revised_reason,
        "conflict": conflict, "claims": claims, "can_propose_revision": False,
        "confirmation_evidence": matches, "contacts": contacts,
    }


def _amendment_blocker(log, terms):
    original = actions._get(log, terms["revises_action_id"])
    prior = delivery_terms(original) if original and sent_order_message(original) else None
    if not prior or (terms_for(original) and not _signed(original, fingerprint(prior))):
        return "amendment needs the original sent order"
    preserved = ("instruction", "delivery_id", "supplier_id", "recipient", "part", "quantity", "unit_price",
                 "total_price", "currency", "incoterm", "delivery_place", "po", "po_line", "emergency_reference",
                 "vehicle_limit_t")
    if any(terms.get(field) != prior.get(field) for field in preserved) or set(prior["conditions"]) - set(terms["conditions"]):
        return "delivery amendment must preserve the original order and vehicle requirement"
    if conflict := delivery_state(original, log=log)["conflict"]:
        return conflict_reason(conflict)
    return amendment_deadline_blocker(prior, terms["deliver_by"])


def preview_confirmations(action):
    if action["kind"] == "cancel_order":
        return {}
    return delivery_state(action)["confirmation_evidence"]


def _confirmation_blocker(matches):
    return next((f"required condition missing: {condition.replace('_', ' ')} ({evidence['reason']})"
                 for condition, evidence in matches.items() if not evidence["confirmed"]), None)


def _ingest_blocker(db, terms):
    issues = db.execute("SELECT path, issue FROM ingest_issues ORDER BY path, issue").fetchall()
    if not issues:
        return None
    supplier, part, po, delivery = (terms[key] for key in ("supplier_id", "part", "po", "delivery_id"))
    refs = {ref for ref in (supplier, part, po, delivery) if ref}
    paths = setting("sources", {}).get("paths", {})
    folder = db.execute("SELECT folder FROM suppliers WHERE supplier_id=?", (supplier,)).fetchone()
    if folder and folder[0]:
        refs.add(folder[0])
    relevant_paths = {path for path, in db.execute(
        """SELECT source FROM purchase_orders WHERE po_number=? OR supplier_id=? OR material_id=?
           UNION SELECT path FROM documents WHERE supplier_id=? OR po_number=?
           UNION SELECT path FROM doc_facts WHERE json_extract(payload,'$.supplier_id')=?
               OR json_extract(payload,'$.material_id')=? OR json_extract(payload,'$.po_number')=?""",
        (po, supplier, part, supplier, po, supplier, part, po)) if path}
    related_shipments = db.execute(
        """SELECT shipment_id, reference FROM shipments WHERE shipment_id=? OR supplier_id=?
           OR instr('|' || coalesce(material_ids,'') || '|', '|' || ? || '|') > 0
           OR instr('|' || coalesce(po_numbers,'') || '|', '|' || ? || '|') > 0""",
        (delivery, supplier, part, po)).fetchall()
    refs.update(value for shipment in related_shipments for value in shipment if value)
    identifiers = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(ref) for ref in sorted(refs)) +
                             r")(?![A-Za-z0-9])", re.I)
    relevant_paths.update(source for entity, source in db.execute("SELECT entity_id, source FROM observations")
                          if entity and source and identifiers.search(entity))
    relevant_paths = {path.partition("#")[0] for path in relevant_paths}
    # These loaders supply the gate's evidence. An unscoped loader failure can leave partial rows.
    loaders = {"master", "suppliers", "documents", "shipments"}
    shared = {"components", "component_updates", "shipments"}
    if po:
        loaders.update(("pos", "receipts"))
        shared.update(("purchase_orders", "receipts"))
    for path, issue in issues:
        source = (path or "").partition("#")[0]
        scoped = identifiers.search(f"{path} {issue}") or source in relevant_paths
        if scoped or source in loaders or any(fnmatchcase(source, paths[key]) for key in shared if paths.get(key)):
            return f"evidence stale: ingest issue in {path or 'unknown source'}"
    return None


def _file_blocker(action: dict, terms: dict) -> str | None:
    supplier, part = terms["supplier_id"], terms["part"]
    po = _values(terms["po"])
    instruction = terms["instruction"]
    shipment_kind = "order" if instruction == "dispatch" else instruction
    with _master() as db:
        if reason := _ingest_blocker(db, terms):
            return reason
        if reason := rules.shipment_commitment_blocker(f"ship-{terms['delivery_id']}", shipment_kind, po):
            return reason
        if reason := rules.shipment_commitment_blocker(action["finding_id"], shipment_kind, po):
            return reason
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
            if _expired({"valid_until": offer.get("valid_until"), "latest_useful_at": None if terms.get("revises_action_id") else _latest_offer_release(offer)}):
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
        if terms.get("revises_action_id"):
            if prior.get("revises_action_id") == terms["revises_action_id"]:
                return other["id"]
            continue
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
    reason = proposal_blocker(action, check_departure=False)
    if reason:
        return False, reason
    try:
        terms = terms_for(action)
        with actions.tx() as log:
            if terms["instruction"] == "cancel_order":
                if action["status"] not in {"approved", "sent"} or not _signed(action, fingerprint(terms)):
                    return False, "verified approval for this cancellation is missing"
                reason = _cancellation_authorization(log, terms)
                return reason is None, reason
            if action["payload"].get("repeat_commitment"):
                if action["status"] not in {"auto", "sent"} or not _authorized_repeat(log, action, terms):
                    return False, "repeat needs the exact terms of an explicitly approved and sent commitment"
                origin = actions._get(log, action["payload"]["repeat_commitment"])
                if conflict := delivery_state(origin, log=log)["conflict"]:
                    return False, conflict_reason(conflict)
            else:
                if terms.get("revises_action_id") and (reason := _amendment_blocker(log, terms)):
                    return False, reason
                if other := _duplicate(log, action, terms):
                    return False, f"duplicate of {other}; the commitment already went out or delivery is unconfirmed"
                if action["status"] not in {"approved", "sent"} or not _signed(action, fingerprint(terms)):
                    return False, "verified approval for these exact terms is missing"
            if not _fresh(log):
                return False, "evidence stale"
            file_blocker = _file_blocker(action, terms)
            if file_blocker and file_blocker.startswith("evidence stale"):
                return False, file_blocker
            state = delivery_state(action, log=log)
            matches = state["confirmation_evidence"]
            if matches and matches != action["payload"].get("confirmation_evidence"):
                log.execute("UPDATE actions SET payload=json_set(payload,'$.confirmation_evidence',json(?)) WHERE id=?",
                            (json.dumps(matches), action["id"]))
            reason = state["hold_reason"] or file_blocker
            if not reason and terms.get("revises_action_id"):
                original = _prior_terms(log, terms["revises_action_id"])
                arrival = rules.ts(state["eta"])
                if arrival and arrival <= rules.ts(original["deliver_by"]):
                    reason = "verified ETA is outside the amendment arrival window"
            return reason is None, reason
    except (ArithmeticError, sqlite3.Error, ValueError, TypeError):
        return False, "evidence stale or unreadable"


def decision_state(action: dict) -> dict:
    held = action["held_reason"]
    blocker = proposal_blocker(action) if applies(action) and not action["payload"].get("repeat_commitment") else None
    if held == DEPARTURE_HOLD and action["status"] in {"proposed", "scheduled", "escalated"}:
        held = blocker
    expired = action["status"] == "expired" or bool(blocker and "expired" in blocker) or bool(held and "expired" in held)
    replaced = re.search(r"duplicate of (ACT-[A-Z0-9]+)", held or "")
    if action["status"] == "superseded":
        terminal = f"Replaced by {replaced[1]}" if replaced else "Replaced"
    elif expired:
        terminal = "Expired — no longer useful"
    else:
        terminal = None
    reason = blocker if blocker == DEPARTURE_HOLD else held or blocker
    terms = terms_for(action)
    matches = action["payload"].get("confirmation_evidence", {})
    if reason == DEPARTURE_HOLD:
        owner, clears = ("Carrier and receiving team",
                         "Verify the truck departed by the deadline and provide a current ETA within the delivery window and confirmed dock slot.")
    elif reason and reason.startswith("evidence stale"):
        owner, clears = "Dockside operator", "Resolve file ingest issues and refresh the portal and mailbox evidence."
    elif reason and reason.startswith("required condition missing"):
        condition = reason.partition(": ")[2].partition(" (")[0]
        owner = "Carrier" if condition == "transport confirmed" else "Receiving team"
        delivery = (terms or {}).get("delivery_id")
        clears = (f"Delivery {delivery}. " + ". ".join(confirmation_summary(matches)) if matches else
                  f"Confirm the {condition.removesuffix(' confirmed')} for {delivery} in the linked email thread.")
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
            "hold_clears": clears if reason and not terminal else None, "confirmation_evidence": matches}
