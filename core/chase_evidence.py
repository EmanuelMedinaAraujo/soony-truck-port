"""Deterministic attribution and reconciliation of Dockside's purchase-order evidence."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field

from core import ingest, rules


@dataclass(frozen=True)
class Evidence:
    path: str
    quote: str


@dataclass(frozen=True)
class Issue:
    kind: str
    question: str
    evidence: tuple[Evidence, ...]
    review: bool = False


@dataclass
class Line:
    po: dict
    supplier: dict
    name: str
    category: str | None
    priority: bool
    need: dt.date | None
    qty: float | None
    revision: str | None
    issues: dict[str, Issue] = field(default_factory=dict)
    adequate: bool = False
    confirmation_evidence: tuple[Evidence, ...] = ()

    @property
    def key(self):
        return self.po["supplier_id"], self.po["po_number"], self.po["po_line"], self.po["material_id"]

    @property
    def evidence(self):
        supplier, order, line, material = self.key
        return Evidence(self.po["source"] or "purchase_orders",
                        f"{order}, line {line}, {supplier}, {material}, quantity {self.po['quantity']}, "
                        f"requested {self.po['requested_date']}, ERP promise {self.po['promised_date']}")

    def add(self, kind, question, *evidence, review=False):
        old = self.issues.get(kind)
        combined = (old.evidence if old else (self.evidence,)) + evidence
        self.issues[kind] = Issue(kind, old.question if old else question, tuple(dict.fromkeys(combined)),
                                  review or bool(old and old.review))


@dataclass(frozen=True)
class Claim:
    evidence: Evidence
    qty: float | None = None
    date: dt.date | None = None
    revision: str | None = None
    amended: bool = False


def day(value) -> dt.date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].+)?", value):
        return None
    try:
        return dt.datetime.fromisoformat(value).date()
    except ValueError:
        return None


def quantity(value) -> float | None:
    number = ingest.num(value)
    return number if number is not None and math.isfinite(number) and number >= 0 else None


def name_key(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def line_number(value):
    text = str(value or "")
    if not text.isdecimal():
        return None
    number = int(text)
    return number // 10 if len(text) == 5 and text.startswith("0") and number % 10 == 0 else number


# The portal acknowledgements have a bilingual heading, a Your order reference, and
# fixed-width line tables. Wrapped description cells are part of the same line.
ACK = re.compile(r"order (?:confirmation|acknowledg[e]?ment)|potwierdzenie zamówienia|주문 확인서|订单确认书", re.I)
AMENDED = re.compile(r"\b(?:amend(?:ed|ment)?|revised|updated|replacement)\b|更正|수정|korekta", re.I)
ORDER_REF = re.compile(r"\bYour order\s*[:#]?\s*(\d+)\b", re.I)
ITEM_ROW = re.compile(
    r"^[ \t]*(\d+)[ \t]{2,}(\S+)[ \t]{2,}(.+?)[ \t]{2,}([\d.,\u00a0 ]+)[ \t]{2,}\S+[ \t]{2,}[A-Z]{3}[ \t]{2,}(\S+)[ \t]*$",
    re.M,
)


def acknowledgements(doc, line, aliases):
    """Return attributable claims and unreadable evidence for this particular PO line."""
    path, text = doc["path"], doc["text"] or ""
    header = next((row.strip() for row in text.splitlines()[:12] if ACK.search(row)), "")
    looks_like_ack = re.match(r"OA\d", path.rsplit("/", 1)[-1], re.I)
    if not header and not looks_like_ack:
        return [], []
    references = set(ORDER_REF.findall(text))
    if not header or references != {line.key[1]} or doc["po_number"] not in (None, line.key[1]):
        return [], [Evidence(path, header or path.rsplit("/", 1)[-1])]
    amended = bool(AMENDED.search(header) or AMENDED.search(path.rsplit("/", 1)[-1].replace("_", " ")))
    claims, unknown = [], []
    for match in ITEM_ROW.finditer(text):
        item, material, description, qty, confirmed = match.groups()
        if line_number(item) != line.key[2]:
            continue
        if material != line.key[3]:
            start, end = match.start(3) - match.start(), match.start(4) - match.start()
            for row in text[match.end():].lstrip("\n").splitlines():
                if not row[:start].isspace() or row[end:].strip() or not row[start:end].strip():
                    break
                description += " " + row[start:end].strip()
            if aliases.get(name_key(description)) != {line.key[3]}:
                unknown.append(Evidence(path, match.group().strip()))
                continue
        claims.append(Claim(Evidence(path, match.group().strip()), quantity(qty), day(confirmed), amended=amended))
    if not claims and not unknown:
        unknown.append(Evidence(path, header))
    return claims, unknown


def _arrival(fact, field):
    value = fact.get(field)
    if not isinstance(value, str):
        return None
    days = {day(token) for token in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", value)}
    return next(iter(days)) if len(days) == 1 else None


def _assess(line, docs, facts, aliases, master_revision):
    if line.need is None or line.qty is None or not line.name or not line.supplier.get("contact_email"):
        line.add("needs_review", "Verify the PO need date, ordered quantity, part name and supplier contact.", review=True)
        return

    claims, unknown, supporting = [], [], []
    for doc in docs:
        found, unclear = acknowledgements(doc, line, aliases)
        claims.extend(found)
        unknown.extend(unclear)

    for kind, fact, evidence in facts:
        supporting.append(evidence)
        if kind in {"order_confirmation", "amended_confirmation", "delivery_promise", "artwork_revision"}:
            claims.append(Claim(evidence, quantity(fact.get("qty")) if kind != "artwork_revision" else None,
                                day(fact.get("arrival_date") or fact.get("delivery_date")) or _arrival(fact, "arrival_note"),
                                str(fact["revision"]).strip().upper() if fact.get("revision") else None,
                                kind == "amended_confirmation" or fact.get("amended") is True))
        if kind == "delivery_promise" and (dispatch := day(fact.get("dispatch_date"))) and dispatch > line.need:
            line.add("late_date", "Confirm the earliest arrival date and quantity available by the need date.", evidence)
        if kind == "shipping_advice":
            ordered, shipped, balance = (quantity(fact.get(key)) for key in ("qty_ordered", "qty_shipped", "balance"))
            if ordered is not None and ordered != line.qty:
                line.add("contradicting_qty", "Confirm the ordered quantity and the quantity still due.", evidence)
            if (balance is not None and balance > 0) or (ordered == line.qty and shipped is not None and shipped < ordered):
                line.add("partial_quantity", "Confirm the quantity shipped and when the remaining units can arrive.", evidence)
                if (eta := _arrival(fact, "balance_eta")) and eta > line.need:
                    line.add("late_date", "Confirm the earliest arrival date and quantity available by the need date.", evidence)
        if kind == "receipt_correction":
            typed, corrected = (quantity(fact.get(key)) for key in ("qty_typed", "qty_corrected"))
            if typed is not None and corrected is not None and typed != corrected:
                line.add("contradicting_qty", "Review the corrected receipt quantity against the PO and goods receipt.",
                         evidence, review=True)
            elif corrected is None:
                unknown.append(evidence)

    # Dispatch dates and shipped/received quantities describe different events from a
    # delivery commitment. Never compare them as if they were two versions of the same claim.
    promised = day(line.po["promised_date"])
    confirmed_qty = {c.qty for c in claims if c.qty is not None}
    confirmed_dates = {c.date for c in claims if c.date is not None}
    revisions = {c.revision for c in claims if c.revision}
    complete = any(c.qty is not None and c.date is not None for c in claims)
    if unknown or (not complete and claims and (confirmed_qty or confirmed_dates)):
        line.add("needs_review", "Verify the confirmation's line, quantity and delivery date.",
                 *unknown, *(c.evidence for c in claims), review=True)
    elif not claims or (not complete and not (confirmed_qty or confirmed_dates)):
        line.add("missing_confirmation", "Confirm the quantity and delivery date for this order line.", *supporting)
    if amended := [c.evidence for c in claims if c.amended]:
        line.add("amended_confirmation", "Confirm which acknowledgment applies and what changed.", *amended)
    if len(confirmed_qty) > 1 or any(qty > line.qty for qty in confirmed_qty):
        line.add("contradicting_qty", "Confirm the applicable quantity and whether any quantity is outstanding.",
                 *(c.evidence for c in claims if c.qty is not None))
    if any(qty < line.qty for qty in confirmed_qty):
        line.add("partial_quantity", "Confirm the quantity available and when the remaining units can arrive.",
                 *(c.evidence for c in claims if c.qty is not None and c.qty < line.qty))
    if len(confirmed_dates | ({promised} if promised else set())) > 1:
        line.add("contradicting_date", "Confirm which delivery date applies.",
                 *(c.evidence for c in claims if c.date))
    if any(date > line.need for date in confirmed_dates | ({promised} if promised else set())):
        line.add("late_date", "Confirm the earliest arrival date and quantity available by the need date.",
                 *(c.evidence for c in claims if c.date and c.date > line.need))
    expected = line.revision or master_revision
    if len(revisions) > 1 or (revisions and expected and revisions != {expected}):
        line.revision = expected
        line.add("contradicting_revision", "Confirm which artwork or drawing revision will be supplied.",
                 *(c.evidence for c in claims if c.revision))
    if line.revision and not revisions:
        line.add("needs_review", "Verify the required artwork or drawing revision against the supplier documents.",
                 *(c.evidence for c in claims), review=True)
    line.adequate = complete and not line.issues
    line.confirmation_evidence = tuple(dict.fromkeys(c.evidence for c in claims))


@dataclass
class Snapshot:
    lines: list[Line]
    orders: dict[tuple, dict]
    shipment_orders: dict[str, list[str]]


def detect() -> Snapshot:
    """Read the current master snapshot. Supplier text and extracted facts are evidence, never instructions."""
    deadline = rules.ts(rules.cfg("deadline")).date()
    only_launch = rules.cfg("chase")["only_launch_relevant"]
    with closing(sqlite3.connect(rules.DB_PATH.as_uri() + "?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN")
        order_rows = defaultdict(list)
        for row in c.execute("SELECT * FROM purchase_orders ORDER BY source"):
            order_rows[tuple(row[k] for k in ("supplier_id", "po_number", "po_line", "material_id"))].append(dict(row))
        relevant_fields = ("status", "quantity", "requested_date", "promised_date", "plant")
        conflicts = {key for key, rows in order_rows.items()
                     if len({tuple(row[field] for field in relevant_fields) for row in rows}) > 1}
        orders = {key: next((row for row in rows if row["status"] == "open"), rows[0]) for key, rows in order_rows.items()}
        by_po = defaultdict(list)
        for key in orders:
            by_po[key[1]].append(key)
        products = {row["product_id"]: dict(row) for row in c.execute("SELECT * FROM products WHERE launch_date=?", (deadline.isoformat(),))}
        priority = rules.cfg("priority_product")
        components = {row["component_id"]: dict(row) for row in c.execute("SELECT * FROM components")}
        launch_bom = defaultdict(list)
        for row in c.execute("SELECT * FROM bom ORDER BY rev_effective_from"):
            if row["parent_id"] in products and (not row["rev_effective_from"] or (day(row["rev_effective_from"]) or dt.date.max) <= deadline):
                launch_bom[row["child_id"]].append(dict(row))
        suppliers = {row["supplier_id"]: dict(row) for row in c.execute("SELECT * FROM suppliers")}
        shipment_orders = {f"ship-{sid}": (pos or "").split("|")
                           for sid, pos in c.execute("SELECT shipment_id,po_numbers FROM shipments")}
        receipts = defaultdict(set)
        for po, line_no, supplier, material, note in c.execute(
                "SELECT po_number,po_line,supplier_id,material_id,delivery_note FROM goods_receipts WHERE delivery_note IS NOT NULL"):
            receipts[po, note].add((supplier, po, line_no, material))
        aliases = defaultdict(lambda: defaultdict(set))
        for row in c.execute("SELECT supplier_id,component_id,description FROM supplier_components"):
            aliases[row["supplier_id"]][name_key(row["description"] or "")].add(row["component_id"])
        docs = defaultdict(list)
        for row in c.execute("SELECT * FROM documents WHERE doc_type='order_confirmations' ORDER BY path"):
            refs = set(ORDER_REF.findall(row["text"] or ""))
            for po in refs | {row["po_number"]}:
                if po:
                    docs[row["supplier_id"], po].append(dict(row))

        facts = defaultdict(list)
        uncertain_facts = defaultdict(list)
        current_paths = {row[0] for row in c.execute("SELECT path FROM sources")}
        for path, kind, payload in c.execute("SELECT path,fact_type,payload FROM doc_facts ORDER BY path, fact_type, payload"):
            if kind not in {"delivery_promise", "shipping_advice", "artwork_revision", "receipt_correction",
                            "order_confirmation", "amended_confirmation"} or (current_paths and path not in current_paths):
                continue
            fact = json.loads(payload)
            po = str(fact.get("po_number") or "")
            path_parts = path.split("/")
            supplier = path_parts[1].split("_")[0] if path_parts[0] == "suppliers" and len(path_parts) > 2 else fact.get("supplier_id")
            candidates = [key for key in by_po[po] if not supplier or supplier == key[0]]
            receipt_keys = receipts.get((po, fact.get("delivery_note") or fact.get("reference")))
            if receipt_keys:
                candidates = [key for key in candidates if key in receipt_keys]
            matching = [key for key in candidates if (not fact.get("material_id") or key[3] == fact["material_id"])
                        and (not fact.get("po_line") or line_number(fact["po_line"]) == key[2])]
            quote = (fact.get("quote") or "").strip()
            if len(matching) == 1 and quote:
                facts[matching[0]].append((kind, fact, Evidence(path, quote)))
            elif quote and (kind != "receipt_correction" or fact.get("qty_corrected") is not None):
                for key in matching or candidates:
                    uncertain_facts[key].append(Evidence(path, quote))

    lines = []
    for key, po in orders.items():
        supplier, _, line_no, material = key
        need = day(po["requested_date"])
        if po["status"] != "open" or (only_launch and (
            po["plant"] != rules.CONFIG["site"] or material not in (launch_bom.keys() | products.keys())
            or (need and need > deadline)
        )):
            continue
        component = components.get(material, {})
        bom = launch_bom[material]
        categories = {row["rev"] for row in bom if row["rev"] and row["use_up_old_rev"] == "N"}
        allow_old = bool(bom and bom[-1]["use_up_old_rev"] == "Y")
        revision = next(iter(categories)) if len(categories) == 1 else None
        master_rev = component.get("drawing_rev") if not allow_old else None
        line = Line(po, suppliers.get(supplier, {}), rules.plain(component.get("description") or products.get(material, {}).get("name")),
                    component.get("category"), any(row["parent_id"] == priority for row in bom) or material == priority,
                    need, quantity(po["quantity"]), revision.strip().upper() if revision else None)
        if not (supplier and po["po_number"] and isinstance(line_no, int) and material):
            continue  # cannot identify the case safely
        if key in conflicts:
            evidence = (Evidence(row["source"] or "purchase_orders",
                                 ", ".join(f"{field}={row[field]}" for field in relevant_fields)) for row in order_rows[key])
            line.add("needs_review", "Reconcile the conflicting PO records before contacting the supplier.", *evidence, review=True)
            lines.append(line)
            continue
        if len(categories) > 1:
            line.add("needs_review", "Verify the conflicting launch drawing requirements.", review=True)
        if uncertain_facts[key]:
            line.add("needs_review", "Identify the PO line to which the supplier evidence applies.", *uncertain_facts[key], review=True)
        _assess(line, docs[supplier, key[1]], facts[key], aliases[supplier],
                master_rev.strip().upper() if master_rev else None)
        lines.append(line)
    return Snapshot(sorted(lines, key=lambda line: (not line.priority, line.need or dt.date.max, line.key)), orders, shipment_orders)
