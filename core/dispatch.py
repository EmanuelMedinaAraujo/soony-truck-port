"""Evidence-backed launch picture and conditional supply recovery scenarios."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
from collections import defaultdict
from contextlib import closing

from core import actions, rules
from core.rules import embedded_components


def forecast(picture: dict, coverage: dict, bom: dict, priority: str, extra: dict | None = None) -> tuple[dict, dict]:
    """Reserve shared stock in priority order, then cap dispatch by every required part and packing capacity."""
    extra = extra or {}
    allocations, parts_by_edition = {}, defaultdict(dict)
    reserved = rules.component_allocations(
        {edition: row["todo_now"] for edition, row in picture.items()},
        {component: max(0, row["usable_now"]) + extra.get(component, 0) for component, row in coverage.items()},
        bom, priority,
    )
    for component, by_edition in reserved.items():
        allocations[component] = {}
        for edition, allocated in by_edition.items():
            per_copy = bom[component][edition]
            required = math.ceil(picture[edition]["todo_now"] * per_copy)
            allocations[component][edition] = {"editionId": edition, "required": required, "allocated": allocated, "perCopy": per_copy}
            if not coverage[component].get("included_in"):
                parts_by_edition[edition][component] = math.floor(allocated / per_copy)

    result = {}
    for edition, row in picture.items():
        capacity = max(0, row["capacity_left"])
        limits = parts_by_edition[edition]
        to_pack = min(row["todo_now"], capacity, *limits.values())
        constraints = [part for part, units in limits.items() if units == to_pack and to_pack < row["todo_now"]]
        projected = row["packed_est_now"] + to_pack
        result[edition] = {"id": edition, "demand": row["demand"], "packed": row["packed_est_now"],
                           "projected": projected, "short": row["demand"] - projected, "constraintIds": constraints,
                           "capacityLimited": capacity == to_pack and to_pack < row["todo_now"]}
    return result, allocations


def evidence(label: str, detail: str) -> dict:
    return {"label": label, "detail": detail}


def step(name: str, state: str, detail: str) -> dict:
    return {"name": name, "state": state, "detail": detail}


def next_step(state: str, text: str, since: str | None = None, action_id: str | None = None) -> dict:
    return {"state": state, "text": text, "since": since, "action_id": action_id}


ACTION_SUBJECTS = {
    "ask": "request", "confirm_terms": "confirmation request", "reserve": "reservation request",
    "order": "order", "book_truck": "truck booking", "dock_slot": "unload slot",
    "expedite": "rush delivery", "quality_release": "quality release", "data_fix": "record change",
    "escalate": "request",
}
INTERNAL_FOLLOWUP = {
    "book_truck": "confirm the truck",
    "dock_slot": "confirm the unload slot",
    "quality_release": "release the stock",
    "escalate": "answer",
    "ask": "answer",
}


def question_options(body: str | None, finding_id: str | None) -> list[dict]:
    """Purchase orders a question names, as answer choices: the line for this part, its quantity and requested date."""
    pattern = ((rules.CONFIG.get("sources") or {}).get("value") or {}).get("document_po_pattern")
    numbers = list(dict.fromkeys(re.findall(rf"\b{pattern}\b", body or ""))) if pattern else []
    part = next(iter(re.findall(r"\b[A-Z]{2,4}-[A-Z0-9]{2,5}-\d{2,4}\b", finding_id or "")), None)
    if not numbers or not rules.DB_PATH.exists():
        return []
    with closing(sqlite3.connect(f"{rules.DB_PATH.resolve().as_uri()}?mode=ro", uri=True)) as db:
        rows = db.execute(f"""SELECT po_number, po_line, quantity, requested_date, status FROM purchase_orders
                               WHERE po_number IN ({",".join("?" * len(numbers))}) AND (? IS NULL OR material_id=?)
                               ORDER BY po_number, po_line""", (*numbers, part, part)).fetchall()
    return [{"po": po, "line": line, "quantity": qty, "requested": requested, "status": status,
             "answer": f"Use PO {po} line {line} for this delivery."} for po, line, qty, requested, status in rows]


def internal_followup(action: dict, message: dict) -> dict:
    """A message that went out by chat or as a call script: say who has to do what next."""
    who = re.split(r"\s+(?:and|\()\s*", message["to_name"] or "", maxsplit=1)[0].strip() or "the team"
    who = re.sub(rf"\b{re.escape(rules.setting('site'))}\b", lambda _: rules.cfg("site_name"), who)
    if message["channel"] == "phone_script":
        call = message.get("call")
        state = "Asking"
        if call and call["result"]:
            result = json.loads(call["result"])
            label = " (simulated, no phone line yet)" if result.get("simulated") else ""
            summary = call["summary_en"] or result.get("summary") or "See the transcript."
            text = f"Dockside called {who}{label}: {_first_sentence(summary.removeprefix('Simulated call: '))}"
            state = "Waiting for reply" if result.get("simulated") else "Asking"
        elif call and call["progress"] == "failed":
            state, text = "Stuck", f"Dockside's call to {who} failed."
        elif not rules.cfg("voice")["enabled"]:
            state, text = "Stuck", f"The call to {who} is unavailable."
        else:
            text = f"Dockside is calling {who} now."
        return next_step(state, text, call["received_at"] if call and call["received_at"] else message["sent_at"],
                         action["id"]) | {"call_id": message["id"]}
    task = INTERNAL_FOLLOWUP[action["kind"]]
    if action["kind"] == "dock_slot":
        return next_step("Waiting for reply", f"Unload slot requested from {who}; waiting for the site to confirm it.", message["sent_at"], action["id"])
    if action["kind"] in ("escalate", "ask"):
        # The request itself says best what the person is asked to do; the short title is the fallback.
        asked = re.sub(r"^(?:(?:hi|hello|hallo)\s+)?(?:[A-Z][\w.-]*(?:\s[A-Z][\w.-]*)?,\s*)?(?:[Pp]lease|[Bb]itte)?\s*", "",
                       _first_sentence(message.get("body"), 140)).rstrip(".!")
        title = asked if asked and not asked.endswith("…") else (action["title"] or "").strip().rstrip(".")
        if title:
            text = f"Waiting for {who}: {title[:1].lower() + title[1:]}" + ("" if title.endswith("?") else ".")
            asked_by = re.search(r"\bby (\d{1,2}:\d{2})\b", message.get("body") or "")
            due = None
            if asked_by and (sent := rules.ts(message.get("sent_at"))):
                local = sent.astimezone(rules.timezone())
                due = local.replace(hour=int(asked_by[1][:-3]), minute=int(asked_by[1][-2:]), second=0, microsecond=0)
                due = due if due >= local else due + dt.timedelta(days=1)
            return next_step("Waiting for reply", text, message["sent_at"], action["id"]) | {
                "person": who, "question": message.get("body"), "message_id": message.get("id"),
                "answer_by": due.isoformat(timespec="minutes") if due else None,
                "options": question_options(message.get("body"), action.get("finding_id"))}
    text = f"Waiting for {who} to {task}."
    return next_step("Waiting for reply", text, message["sent_at"], action["id"]) | {"person": who}


def message_answered(message: dict, replies: list[dict]) -> bool:
    sent = rules.ts(message["sent_at"])
    for reply in replies:
        received = rules.ts(reply["received_at"])
        if reply["msg_id"] in (None, message["id"]) and (not sent or not received or received >= sent):
            return True
    return False


def _first_sentence(text: str | None, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return sentence if len(sentence) <= limit else sentence[:limit - 1].rsplit(" ", 1)[0] + "…"


def simulated_call_summary(text: str | None) -> str:
    label = "simulated call (local mock)"
    text = re.sub(r"^(?:\s*simulated call(?: \(local mock\))?(?:\s*[:—-]\s*|\s*$))+", "", text or "", flags=re.I)
    text = re.sub(r"\bnot (?:verified|confirmed|validated)\b", "still uncertain", text, flags=re.I)
    text = re.sub(r"\bunverified\b", "provisional", text, flags=re.I)
    text = re.sub(r"\b(?:verified|confirmed|validated|established|proved)\b", "reported", text, flags=re.I)
    return f"{label}: {text.strip()}" if text.strip() else label


def readable_activity(log, limit: int = 10) -> dict:
    """Recent work in plain English: who was asked what, what they answered, what changed.
    The raw event log has e-mail addresses and foreign-language subjects; those stay out."""
    rows = log.execute("""SELECT e.ts, e.kind, e.ref, e.text, a.title FROM events e LEFT JOIN actions a ON a.id = e.ref
                          WHERE e.kind IN ('email_sent','approval_email_sent','reply','action_auto','action_approved','action_done','data_drop','docs_read')
                          ORDER BY e.ts DESC, e.rowid DESC LIMIT 60""").fetchall()
    replies = log.execute("""SELECT action_id, from_name, received_at, summary_en, simulated, subject
                             FROM replies ORDER BY julianday(received_at) DESC, rowid DESC""").fetchall()
    sent_to = {r[0]: r[1] for r in log.execute("SELECT action_id, to_name FROM messages WHERE channel='email' ORDER BY created_at")}
    items = []
    emailed = {ref for _, kind, ref, _, _ in rows if kind == "email_sent"}
    since = rules.now() - dt.timedelta(hours=24)
    for ts, kind, ref, text, title in rows:
        title = (title or "").strip().rstrip(".")
        what = title[:1].lower() + title[1:] if title else ""
        if kind == "email_sent":
            who = sent_to.get(ref) or "a supplier"
            line = f"Emailed {who}" + (f" to {what}" if what else "") + "."
        elif kind == "approval_email_sent":
            line = text
        elif kind == "reply":
            moment = rules.ts(ts)
            candidates = [r for r in replies if r[0] == ref and r[2] and rules.ts(r[2]) <= moment]
            reply = next((r for r in candidates if text == f"{r[1]} replied: {r[5]}"), None)
            simulated = " (simulated call) replied:" in text
            if reply is None and ref:
                reply = next((r for r in candidates if bool(r[4]) == simulated), None)
            who = (reply[1] if reply else text.split(" replied", 1)[0]) or "Supplier"
            if (reply and reply[4]) or simulated:
                who = who.removesuffix(" (simulated call)")
                summary = simulated_call_summary(reply[3] if reply else None)
                line = f"{who} — {_first_sentence(summary)}"
            else:
                line = f"{who} replied: {_first_sentence(reply[3])}" if reply and reply[3] else f"{who} replied" + (f" about: {what}" if what else "") + "."
        elif kind == "action_auto":
            line = f"Dockside started: {what}." if what and ref not in emailed else None
        elif kind == "action_approved":
            who = text.split(" (", 1)[0].strip() or "Someone"
            line = f"{who} approved: {what}." if what else None
        elif kind == "action_done":
            line = f"Done: {_first_sentence(text)}"
        elif kind == "data_drop":
            n = re.match(r"(\d+)", text or "")
            line = f"{n.group(1) if n else 'Some'} data files updated."
        else:  # docs_read
            n = re.search(r"(\d+)", text or "")
            line = f"Read {n.group(1)} new documents." if n else None
        if not line:
            continue
        if len(items) < limit and (not items or items[-1]["text"] != line):
            items.append({"ts": ts, "text": line})
    # Count records, not log lines, over the whole window: one email is one message row, one reply one reply row.
    def count(sql: str) -> int:
        return sum(1 for (stamp,) in log.execute(sql) if (moment := rules.ts(stamp)) and moment >= since)
    sent = count("SELECT sent_at FROM messages WHERE channel IN ('email','approval_email') AND status='sent'")
    received = count("SELECT received_at FROM replies WHERE COALESCE(simulated, 0)=0")
    staff = count("SELECT sent_at FROM messages WHERE channel='discord' AND sent_at IS NOT NULL")
    calls = count("SELECT received_at FROM replies WHERE COALESCE(simulated, 0)=1")
    parts = [(sent, "email sent", "emails sent"), (received, "reply received", "replies received"),
             (staff, "message to staff", "messages to staff"),
             (calls, "simulated call (local mock)", "simulated calls (local mock)")]
    summary = ", ".join(f"{n} {one if n == 1 else many}" for n, one, many in parts if n)
    return {"summary": f"Last 24 hours: {summary}" if summary else "Nothing in the last 24 hours", "items": items}


def action_next_step(related: list[dict], supplier: str | None = None) -> dict | None:
    """Choose outstanding work from the log. Sending and replying are separate events."""
    candidates = []
    for action in related:
        status, aid = action["status"], action["id"]
        if status in ("superseded", "rejected"):
            continue
        subject = ACTION_SUBJECTS.get(action["kind"], "request")
        if action["kind"] in ("ask", "confirm_terms") and re.search(r"\bvans?\b", action["title"] or "", re.IGNORECASE):
            subject = "van request"
        updated = action["updated_at"]
        if status in ("proposed", "escalated"):
            candidates.append((0, next_step("Needs you", f"Approve the {subject}.", updated, aid)))
            continue
        if status == "scheduled":
            due = rules.ts(action["next_check_at"])
            timing = f" at {due:%H:%M}" if due else ""
            candidates.append((1, next_step("Asking", f"The {subject} is scheduled for release{timing}.", updated, aid)))
            continue
        if status in ("approved", "auto", "sent"):
            for message in action["messages"]:
                if message["channel"] == "approval_email":
                    continue
                mail_status = message["status"]
                if mail_status in ("sending", "delivery_unknown", "outbox", "draft"):
                    text = {
                        "sending": f"Sending the {subject}.",
                        "delivery_unknown": f"Check whether the {subject} reached its recipient.",
                        "outbox": f"The {subject} is queued for delivery.",
                        "draft": f"The {subject} is ready to send.",
                    }[mail_status]
                    state = "Stuck" if mail_status == "delivery_unknown" else "Asking"
                    candidates.append((1, next_step(state, text, updated, aid)))
                elif (message["channel"] == "email" and mail_status == "sent"
                      and not message_answered(message, action["replies"])):
                    sent_at = message["sent_at"]
                    who = supplier or message["to_name"] or ""
                    when = rules.ts(sent_at)
                    timing = f" at {when.astimezone(rules.timezone()):%H:%M}" if when else ""
                    email_subject = "order request" if action["kind"] == "order" else subject
                    text = f"Sent the {email_subject} to {who}{timing}."
                    if not who or "@" in who or re.search(r"\b(?:CMP|SUP|SHP|DC)-", who) or len(text.split()) > 12:
                        text = f"Sent the {email_subject}{timing}."
                    candidates.append((3, next_step("Waiting for reply", text, sent_at, aid)))
                elif mail_status == "delivered" and (message["channel"] == "phone_script" or action["kind"] in INTERNAL_FOLLOWUP):
                    candidates.append((4, internal_followup(action, message)))
        for reply in action["replies"]:
            if not reply["handled"]:
                if any(m["id"] == reply["msg_id"] and m["channel"] == "phone_script" for m in action["messages"]):
                    continue  # the call step already shows the result and transcript
                who = supplier or "the supplier"
                candidates.append((2, next_step("Asking", f"Dockside is reading the reply from {who}.", reply["received_at"], aid)))

    if not candidates:
        return None
    # Resolve equal-priority work by the latest transition, independent of ISO time-zone offsets.
    priority = min(rank for rank, _ in candidates)
    return max((item for rank, item in candidates if rank == priority),
               key=lambda item: rules.ts(item["since"]) or dt.datetime.min.replace(tzinfo=rules.timezone()))


def shipment_progress(shipment: dict, related: list[dict], supplier: str, hold: str | None, now: dt.datetime) -> tuple[str, dict]:
    status, updated = shipment["status"], shipment["updated_at"]
    arrival = rules.ts(shipment["arrival_at_dc"])
    if status == "delivered":
        return "receive", next_step("Unloaded", "Check receipt and inspection before using the stock.", updated)
    if status == "arrived_dc":
        return "receive", next_step("Confirmed", f"The shipment reached {rules.cfg('site_name')}. Confirm unloading.", updated)

    logged = action_next_step(related, supplier)
    if hold:
        return "confirm", logged or next_step("Stuck", f"Confirm release terms with {supplier}.", updated)
    if status == "arrived_port":
        return "track", logged or next_step("Stuck", f"Arrange a truck from the port to {rules.cfg('site_name')}.", updated)
    if status == "booked":
        if logged:
            return "ship", logged
        reply = max((row for action in related for row in action["replies"] if not row.get("simulated")), key=lambda row: row["received_at"] or "", default=None)
        heard, tracked = (rules.ts(reply["received_at"]) if reply else None), rules.ts(updated)
        if reply and reply.get("summary_en") and heard and (not tracked or heard > tracked):
            # The supplier told us more than the tracker knows: show what they said.
            return "ship", next_step("On the way", f"{supplier} reported at {heard.astimezone(rules.timezone()):%a %H:%M}: "
                                     f"{_first_sentence(reply['summary_en'])} The tracker is not updated yet.", reply["received_at"], reply["action_id"]) | {"unverified": True}
        etd = rules.ts(shipment.get("etd"))
        late = bool(etd and etd.astimezone(rules.timezone()).date() < now.astimezone(rules.timezone()).date())
        text = (f"Pickup was due {etd:%a} and {supplier} has not confirmed it happened." if late else
                f"Booked with {supplier}; pickup not confirmed yet.")
        state = "Late" if late else "Booked"
        if reply:
            return "ship", next_step(state, text, reply["received_at"], reply["action_id"])
        return "ship", next_step(state, text, updated)

    if status in ("in_transit", "in_delivery", "delayed"):
        if shipment["makes_cutoff"] is False and not logged:
            return "track", next_step("After launch", "Arrives after the receiving cutoff, so it does not count for the launch.", updated)
        if logged:
            return "track", logged
        if arrival and arrival < now:
            return "track", next_step("Stuck", "Check with the carrier whether the shipment arrived.", updated)
        if status == "delayed" or any("no carrier update" in flag for flag in shipment["flags"]):
            return "track", next_step("Stuck", "Get a fresh arrival estimate from the carrier.", updated)
        if shipment["makes_cutoff"] is False:
            return "track", next_step("After launch", "Arrives after the receiving cutoff, so it does not count for the launch.", updated)
        return "track", next_step("On the way", f"Track the shipment to {rules.cfg('site_name')}.", updated)
    return "confirm", logged or next_step("Stuck", "Check shipment status with the supplier.", updated)


def coordinates(lat: float | None, lon: float | None) -> dict | None:
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"lat": lat, "lon": lon}


def shipment_tracking(shipment: dict, places: dict, hold: str | None) -> dict:
    origin, terminal = shipment["origin"], shipment["destination"]
    codes = dict.fromkeys([origin, shipment.get("via"), terminal, shipment.get("dest_site")])
    stops = []
    for code in codes:
        place = places.get(code)
        if place and (not stops or coordinates(place["lat"], place["lon"]) != coordinates(stops[-1]["lat"], stops[-1]["lon"])):
            stops.append(place)
    status = shipment["status"]
    position = None
    if not hold and status in ("in_transit", "in_delivery", "delayed", "arrived_port"):
        position = coordinates(shipment["last_lat"], shipment["last_lon"])
    if status == "arrived_port" and not hold and terminal in places:
        place = places[terminal]
        position = coordinates(place["lat"], place["lon"])
    origin_name = places.get(origin, {}).get("name", origin)
    terminal_name = places.get(terminal, {}).get("name", terminal)
    return {"reference": shipment["reference"] or shipment["shipment_id"], "mode": shipment["mode"], "status": status,
            "origin": origin_name, "terminal": terminal_name, "stops": stops, "position": position,
            "updatedAt": shipment["updated_at"], "arrival": shipment["arrival_at_dc"], "hold": hold,
            "stale": any("no carrier update" in flag for flag in shipment["flags"])}


def shipment_route(shipment: dict, suppliers: dict, related: list[dict], places: dict, now: dt.datetime) -> dict:
    flags = shipment["flags"]
    ref = shipment["reference"] or shipment["shipment_id"]
    arrival = shipment["arrival_at_dc"]
    received = shipment["status"] in ("delivered", "arrived_dc")
    at_port = shipment["status"] == "arrived_port"
    review = shipment.get("quantity_review")
    hold = ("Conflicting shipment quantity. Hold for review." if review
            else next((flag for flag in flags if flag.startswith("supplier hold")), None))
    moving = shipment["status"] in ("in_transit", "in_delivery", "arrived_port", "delivered", "arrived_dc")
    uncertain = next((flag for flag in flags if "truck ban" in flag or "no carrier update" in flag), None)
    latest_reply = latest_summary(related)
    transport = "blocked" if hold else "pending" if uncertain or not moving else "confirmed"
    receiving = "confirmed" if received else "blocked" if shipment["makes_cutoff"] is False else "pending"
    if review:
        receiving, receiving_note = "blocked", hold
    elif received:
        receiving_note = "The shipment record shows this load arrived. Usable stock is counted from inventory records."
    elif arrival and shipment["makes_cutoff"] is False:
        receiving_note = f"Expected {rules.ts(arrival).astimezone(rules.timezone()):%a %d %b %H:%M}, after the receiving cutoff."
    elif arrival:
        receiving_note = f"Expected {rules.ts(arrival).astimezone(rules.timezone()):%a %d %b %H:%M}. Receiving is still open."
    else:
        receiving_note = "Arrival and a receiving slot still need confirmation."
    source = suppliers.get(shipment["supplier_id"], "Supplier shipment")
    stage, next_action = shipment_progress(shipment, related, suppliers.get(shipment["supplier_id"], "the supplier"), hold, now)
    if review:
        stage, next_action = "confirm", next_step("Needs you", hold, shipment["updated_at"]) | {"reviewRequired": True}
    mode = {"sea": "Container", "air": "Air freight", "road": "Truck"}.get(shipment["mode"], "Shipment")
    steps = [step("Supplier release", "blocked" if hold else "confirmed" if moving else "pending",
                  hold or ("Dispatch appears in the shipment record." if moving else "The tracker shows a booking. Dispatch has yet to be confirmed."))]
    if at_port:
        steps.extend([step("Port arrival", "confirmed", "The container is at the terminal, ready for pickup."),
                      step("Truck pickup", "blocked" if review else "pending", hold if review else "A carrier pickup still needs confirmation.")])
    else:
        steps.append(step("Transport", transport, hold or uncertain or "The shipment tracker records this leg."))
    steps.append(step("Receiving", receiving, receiving_note))
    sources = [evidence(path.rsplit("/", 1)[-1], f"Shipment {ref}") for path in shipment["evidence"]]
    if review:
        sources = [evidence(claim["source"], f"Reports {claim['value']} units") for claim in review]
    if latest_reply:
        sources.insert(0, evidence(f"Supplier reply from {latest_reply['from_name']}", latest_reply["summary_en"]))
    return {"id": shipment["shipment_id"], "name": f"{mode} from {source}" if source != "Supplier shipment" else mode,
            "quantity": shipment["quantity"] if len(shipment["materials"]) == 1 else None,
            "steps": steps, "evidence": sources, "tracking": shipment_tracking(shipment, places, hold),
            "current_stage": stage, "next_step": next_action}


def linked_actions(conn: sqlite3.Connection) -> dict:
    action_rows = {row["id"]: dict(row) | {"messages": [], "replies": []} for row in conn.execute(
        "SELECT id, finding_id, title, kind, status, updated_at, next_check_at, payload, history FROM actions")}
    call_rows = {row["msg_id"]: dict(row) for row in conn.execute(
        """SELECT v.*, r.summary_en, r.received_at FROM voice_calls v LEFT JOIN replies r ON r.id=v.reply_id""")}
    for message in conn.execute("SELECT id, action_id, channel, to_name, to_address, status, sent_at, provider_id, body FROM messages"):
        if action := action_rows.get(message["action_id"]):
            action["messages"].append(dict(message) | {"call": call_rows.get(message["id"])})
    parent_actions = {}
    for reply in conn.execute("""SELECT id, msg_id, action_id, from_addr, from_name, summary_en, received_at, handled, simulated
                                FROM replies ORDER BY received_at DESC"""):
        if action := action_rows.get(reply["action_id"]):
            action["replies"].append(dict(reply))
            parent_actions[reply["id"]] = action
    grouped = defaultdict(list)
    for action in action_rows.values():
        finding = action["finding_id"] or ""
        visited = set()
        while finding not in visited:  # follow reply-<id> and followup-<root>-<stamp> links back to the root finding
            visited.add(finding)
            if finding.startswith("followup-"):
                finding = finding.removeprefix("followup-").rsplit("-", 1)[0]
            elif finding.startswith("reply-") and (parent := parent_actions.get(finding.removeprefix("reply-"))):
                finding = parent["finding_id"] or ""
            else:
                break
        grouped[finding].append(action)
    return grouped


def latest_summary(related: list[dict]) -> dict | None:
    return max((reply for action in related for reply in action["replies"] if reply["summary_en"] and not reply.get("simulated")),
               key=lambda reply: reply["received_at"] or "", default=None)


def hours_in(text: str | None) -> float | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*h\b", text or "", re.IGNORECASE)
    return float(match[1].replace(",", ".")) if match else None


def offer_price(conn: sqlite3.Connection, offer: dict) -> tuple[float | None, str | None]:
    """Use the quoted euro price or the last EUR PO when the offer explicitly quotes Annex A."""
    if offer.get("unit_price_eur") is not None:
        return offer["unit_price_eur"], None
    source = conn.execute("SELECT text FROM documents WHERE path=?", (offer["_path"],)).fetchone()
    if not source or "Annex A price" not in " ".join((source[0] or "").split()):
        return None, None
    price = conn.execute("""SELECT unit_price, source FROM purchase_orders
                            WHERE supplier_id=? AND material_id=? AND currency='EUR' AND unit_price > 0
                            ORDER BY order_date DESC LIMIT 1""", (offer["supplier_id"], offer["material_id"])).fetchone()
    return tuple(price) if price else (None, None)


def _clock(moment: dt.datetime | None) -> str:
    return f"{moment.astimezone(rules.timezone()):%a %H:%M}" if moment else ""


def _requested_arrival(delivery: dict) -> dt.datetime | None:
    return rules.ts(delivery.get("deliver_by"))


def departure_state(delivery: dict | None, _now: dt.datetime) -> dict | None:
    return (delivery or {}).get("departure")


def offer_steps(related: list[dict], supplier: str, quantity: int, offer: dict, expired: bool,
                order: dict | None, delivery: dict):
    """Show which delivery requirements have verified evidence and which are still open."""
    site = rules.cfg("site_name")
    steps = [step("Supplier stock", "confirmed", f"{supplier} offered {quantity:,} units.")]
    from core import questions, release

    sent = release.sent_order_message(order) if order else None
    if sent:
        sent_at = sent["sent_at"]
        names = {m.get("to_name") for m in order["messages"] if m.get("to_name")}
        claims = delivery["claims"]
        accepted = next((row for row in reversed(claims) if row["kind"] == "accepted"), None)
        conflict = delivery["conflict"]
        label = "Delivery amendment" if delivery.get("revises_action_id") else "Order"
        entries = order.get("history") or []
        entries = json.loads(entries) if isinstance(entries, str) else entries
        history = " ".join(str(entry.get("entry", "")) for entry in entries if isinstance(entry, dict))
        how = " when its stop window ran out, without a signature (the policy before the release gate)" if "unless" in history and "stop" in history.lower() else ""
        # One sourced row per event: what we sent, and what the supplier said (without the agent's own notes).
        events = [{"message_id": sent["id"], "at": sent_at, "who": "Dockside", "kind": "sent",
                   "text": f"{label} sent{how}."}]
        for action in related:
            if action["id"] == order["id"]:
                continue
            for m in action.get("messages", []):
                if (m.get("channel") == "email" and m.get("to_name") in names and m.get("sent_at")
                        and m["sent_at"] > sent_at and m.get("status") in {"sent", "delivered"}):
                    events.append({"message_id": m["id"], "at": m["sent_at"], "who": "Dockside", "kind": "sent",
                                   "text": f"Follow-up to {m['to_name']}: {(action.get('title') or 'reminder').rstrip('.')}."})
        events.extend(dict(claim) for claim in claims)
        events.sort(key=lambda event: rules.ts(event["at"]))
        for event in events:
            event["at"] = _clock(rules.ts(event["at"]))
        if conflict:
            at = rules.ts(conflict["at"]).astimezone(rules.timezone())
            summary = f"Disputed by {conflict['who']} at {at:%H:%M}."
        elif accepted:
            summary = f"Accepted by {accepted['who']} {_clock(rules.ts(accepted['at']))}."
        else:
            summary = f"Sent {_clock(rules.ts(sent_at))}. Awaiting acceptance."
        steps.append(step("Formal order", "confirmed" if accepted and not conflict else "waiting", summary) | {"events": events})
    else:
        steps.append(step("Formal order", "waiting" if order else "pending",
                          ("Delivery amendment" if delivery.get("revises_action_id") else "Order") +
                          " awaits release." if order else "Release the order after transport and receiving are confirmed."))

    carrier = (offer.get("transport") or "").split(",")[0].strip() or "the carrier"
    transport = delivery.get("confirmation_evidence", {}).get("transport_confirmed", {})
    dock = delivery.get("confirmation_evidence", {}).get("dock_slot_confirmed", {})

    def reported(match):
        return f"{match['contact']} · {_clock(rules.ts(match['received_at']))}"

    waiting = "waiting" if order else "pending"
    booking = delivery.get("booking_ref")
    steps.append(step("Carrier booking", "confirmed" if booking else waiting,
                      f"Booking {booking} confirmed by {reported(transport)}." if booking else
                      f"Awaiting a booking reference from {carrier} or the supplier's authorized logistics contact."))
    eta = rules.ts(delivery.get("eta"))
    requested = _requested_arrival(delivery)
    needed = f" Ordered arrival {_clock(requested)}." if requested else ""
    missed = delivery.get("needs_revised_terms")
    steps.append(step("Arrival time", "blocked" if missed else "confirmed" if eta and not transport.get("eta_expired") else waiting,
                      (f"ETA {_clock(eta)} from {reported(transport)}." if eta else "Awaiting a verified carrier ETA.") +
                      needed + (f" {delivery['revised_reason']}" if missed else "")))
    window = delivery.get("slot_window")
    if window:
        detail = f"{_clock(rules.ts(window['start']))}–{rules.ts(window['end']).astimezone(rules.timezone()):%H:%M} confirmed by {reported(dock)}."
        if not delivery["slot_confirmed"]:
            reason = dock["reason"] or "verify a slot against the reconciled ETA"
            detail += f" {reason[0].upper() + reason[1:]}."
    else:
        detail = (f"Request a new unload window with {site} for the revised arrival." if missed or delivery.get("revises_action_id")
                  else f"Awaiting confirmation of the unload window from {site}.")
    slot_state = "confirmed" if delivery.get("slot_confirmed") else "blocked" if window and eta else waiting
    steps.append(step("Unload slot", slot_state, detail))
    limit = delivery.get("vehicle_limit")
    if limit is None:
        steps.append(step("Vehicle", "pending", "Awaiting any vehicle requirements in the order terms."))
    elif delivery["vehicle_ok"]:
        steps.append(step("Vehicle", "confirmed",
                          f"Rated {delivery['vehicle_rating']:g} t gross, confirmed by {reported(transport)}. Order limit ≤{limit:g} t."))
    else:
        steps.append(step("Vehicle", waiting, f"Awaiting carrier confirmation that the vehicle is rated ≤{limit:g} t gross."))
    task = delivery.get("task")
    reminders = questions.reminders() if related else {}
    asked, history = {}, []
    for action in sorted(related, key=lambda a: a.get("updated_at") or "", reverse=True):
        if action["kind"] not in {"ask", "escalate", "chase_escalation"}:
            continue
        delivery_id = json.loads(action["payload"] or "{}").get("delivery_id")
        if order and delivery_id and delivery_id not in {order["id"], delivery["delivery_id"]}:
            continue
        for message in action["messages"]:
            if message["channel"] != "discord" or message["status"] != "delivered":
                continue
            person = questions.person(message["to_name"])
            when = _clock(rules.ts(message["sent_at"]))
            if task_id := reminders.get(message["id"]):
                notice = "Escalation" if action["kind"] in {"escalate", "chase_escalation"} else "Reminder"
                history.append(step(f"{notice} to {person}", "superseded",
                                    f"{notice} sent {when}. See task {task_id}.") | {
                    "person": person, "question": message["body"], "since": message["sent_at"],
                    "superseded_message_id": message["id"], "reminder_of": task_id})
            elif (action["status"] not in {"superseded", "rejected", "expired"}
                  and person not in asked and not message_answered(message, action["replies"])):
                pending = internal_followup(action, message)
                asked[person] = step(f"Answer from {person}", "waiting",
                                     f"{pending['text']} Asked {when}.") | {
                    key: pending.get(key) for key in ("message_id", "question", "answer_by", "since", "options")} | {"person": person}
    if task:
        owner = task["person"]
        opened = task["status"] == "open"
        steps.append(step(f"Task for {owner}", "waiting" if opened else "answered",
                          "Reconcile the order and its dispatch instruction with the supplier, then record the outcome."
                          if opened else _awaiting_task_evidence(task)) | {
            "task": task, "person": owner, "answerable_by": task["answerable_by"], "since": delivery["task_created_at"],
            "message_id": task["id"] if opened else None, "question": task["text"] if opened else None,
            "options": [
                {"answer": questions.ORDER_STANDS, "label": "Record: order stands and dispatch instruction confirmed in writing"},
                {"answer": questions.CANCEL_ORDER, "label": "Record: cancel the order"},
            ] if opened else []})
    unique = steps + list(asked.values()) + history
    unique.append(step("Receiving", "blocked" if expired else "pending", "Add to usable stock after receipt and inspection."))
    return unique


def _awaiting_task_evidence(task):
    at = _clock(rules.ts(task["answered_at"]))
    who = task["answered_by"] or task["person"]
    return f"Answered by {who} at {at} — waiting for supplier confirmation and logistics evidence."


def recovery(conn, offer, suppliers, related, alt, baseline, cutoff, now):
    component = offer["material_id"]
    quantity = int(offer["qty"])
    supplier = suppliers.get(offer["supplier_id"], "Supplier")
    unit_price, price_source = offer_price(conn, offer)
    cost = round(unit_price * quantity, 2) if unit_price is not None else None
    load, drive = hours_in(offer.get("lead_time_note")), hours_in(offer.get("transport"))
    latest_release = cutoff - dt.timedelta(hours=load + drive) if load is not None and drive is not None else None
    valid = offer.get("valid_until")
    try:
        valid_day = dt.date.fromisoformat(valid[:10]) if valid else None
    except ValueError:
        valid_day = None
    from core import questions, release

    orders = [actions.get(a["id"]) for a in related if a["kind"] == "order"
              and a["status"] not in {"expired", "superseded", "rejected"}]
    orders = [a for a in orders if a and release.delivery_terms(a) and not a["payload"].get("repeat_commitment")]
    sent = [rules.ts(m["sent_at"]) for a in orders for m in a["messages"]
            if m["channel"] == "email" and m["status"] in {"sent", "delivered"} and m["sent_at"]]
    released = min(sent, default=None)
    pending_revision = max((a for a in orders if release.terms_for(a) and release.terms_for(a).get("revises_action_id")
                            and not release.sent_order_message(a)), key=lambda a: a["created_at"], default=None)
    order = max((a for a in orders if not (release.terms_for(a) or {}).get("revises_action_id")
                 or release.sent_order_message(a)),
                key=lambda a: (bool(release.sent_order_message(a)), a["created_at"]), default=None)
    requested_by = rules.ts(release.delivery_terms(order).get("deliver_by")) if order else None
    depart_requested = requested_by - dt.timedelta(hours=drive) if requested_by and drive is not None else None
    depart_cutoff = cutoff - dt.timedelta(hours=drive) if drive is not None else None
    delivery = questions.order_state(order, depart_for_requested=depart_requested, now=now) if order else {}
    expired = now >= cutoff or (not released and ((latest_release and now > latest_release) or (valid_day and now.date() > valid_day)))
    timing = "expired" if expired else "released" if released else "open" if latest_release else "unknown"
    origin = (re.search(r"\b([A-ZÀ-Ž][\wÀ-ž]+)\s*[–-]\s*[A-ZÀ-Ž]", offer.get("transport") or "") or [None, None])[1]
    contact = conn.execute("SELECT contact_email FROM suppliers WHERE supplier_id=?", (offer["supplier_id"],)).fetchone()
    addresses = {(contact[0] or "").casefold()} if contact else set()
    if order:
        addresses.add(release.delivery_terms(order)["recipient"])
    active = max((reply for a in related for reply in a["replies"]
                  if not reply.get("simulated") and reply.get("from_addr")
                  and reply["from_addr"].casefold() in addresses
                  and (received := rules.ts(reply["received_at"])) and received <= now),
                 key=lambda reply: rules.ts(reply["received_at"]), default=None)
    conflict = delivery.get("conflict")
    modeled_unload = modeled_basis = anchor = anchor_reply_id = late = None
    if load is not None and drive is not None:
        if conflict:
            anchor = rules.ts(conflict["at"])
            anchor_reply_id = conflict["message_id"]
            basis = "the dispute reply"
        elif active:
            anchor = rules.ts(active["received_at"])
            anchor_reply_id = active["id"]
            basis = "the latest supplier reply"
        else:
            anchor = now
            basis = "now"
        anchor = anchor.astimezone(rules.timezone())
        modeled_unload = (anchor.astimezone(dt.timezone.utc) + dt.timedelta(hours=load + drive)).astimezone(rules.timezone())
        seconds_late = (modeled_unload.astimezone(dt.timezone.utc) - cutoff.astimezone(dt.timezone.utc)).total_seconds()
        late = max(0, round(seconds_late / 60))
        modeled_basis = f"If loading takes {load:g} h from {basis} at {anchor:%H:%M} plus {drive:g} h drive"
    if conflict:
        timing = "disputed"
        if modeled_unload and seconds_late > 0:
            delivery["revised_reason"] += f" Modeled unload at {modeled_unload:%H:%M} misses the {cutoff:%H:%M} receiving cutoff."
    elif delivery.get("needs_revised_terms"):
        delivery["pending_revision_action_id"] = pending_revision["id"] if pending_revision else None
        later = f"A later arrival (latest {cutoff:%H:%M} receiving cutoff"
        if origin and depart_cutoff:
            later += f", leave {origin} by {depart_cutoff:%H:%M}"
        later += f") needs revised terms with {supplier}, a carrier ETA and a new unload slot."
        delivery["revised_reason"] += " " + later
        eta = rules.ts(delivery.get("eta"))
        delivery["can_propose_revision"] = bool(not expired and (
            eta <= cutoff if eta and eta >= now else depart_cutoff and now <= depart_cutoff))
    priority = rules.cfg("priority_product")
    accounting = rules.cfg("missed_copy_cost_eur")
    deltas = {edition: row["projected"] - baseline[edition]["projected"] for edition, row in alt.items()}
    target = max(baseline, key=lambda edition: (deltas[edition] * accounting.get(edition, 0), deltas[edition], edition == priority))
    copies = deltas[target]
    avoided = round(sum(delta * accounting.get(edition, 0) for edition, delta in deltas.items()), 2)
    sources = [evidence(offer["_path"].rsplit("/", 1)[-1], offer.get("summary") or "Supplier offer")]
    if price_source:
        sources.append(evidence(price_source.rsplit("/", 1)[-1], "The offer points to Annex A; estimate uses the latest EUR purchase-order unit price."))
    if claim := next((row for row in reversed(delivery.get("claims", [])) if row["kind"] not in {"estimate", "note"}), None):
        sources.insert(0, evidence(f"Supplier reply from {claim['who']}", claim["text"]) | {"message_id": claim["message_id"]})
    elif active and active["summary_en"]:
        sources.insert(0, evidence(f"Supplier reply from {active['from_name']}", active["summary_en"]))
    steps = offer_steps(related, supplier, quantity, offer, expired, order, delivery)
    constraints = alt[target]["constraintIds"]
    next_constraint = constraints[0] if constraints else "Packing capacity" if alt[target]["capacityLimited"] else None
    task = delivery.get("task")
    reminder_ids = {row["superseded_message_id"] for row in steps if row.get("reminder_of")}
    logged = action_next_step([a | {"messages": [m for m in a["messages"] if m["id"] not in reminder_ids]}
                               for a in related], supplier) if not task else None
    if task:
        answered = task["status"] == "answered"
        next_action = next_step("Waiting for reply" if answered else "Needs you",
                                _awaiting_task_evidence(task) if answered else delivery["revised_reason"],
                                task["answered_at"] if answered else delivery["task_created_at"], delivery["action_id"]) | {
            "urgent": True, "person": task["person"], "reply_id": conflict["message_id"], "task": task}
    elif expired:
        next_action = next_step("Stuck", "The rush order window has passed.")
    elif delivery.get("needs_revised_terms"):
        next_action = next_step("Stuck", delivery["revised_reason"], action_id=delivery["action_id"]) | {"urgent": True}
    elif delivery.get("hold_reason"):
        next_action = next_step("Waiting for reply", delivery["hold_reason"], order["updated_at"], order["id"])
    elif delivery.get("transport_confirmed") and delivery.get("slot_confirmed"):
        next_action = next_step("Confirmed", "Carrier and receiving confirmed the delivery arrangements.")
    elif logged:
        next_action = logged
    else:
        # The supplier has answered but transport and the unload slot are still open: say who we wait for.
        heard = rules.ts(active["received_at"]) if active else None
        last = f" (last answer {heard.astimezone(rules.timezone()):%H:%M})" if heard else ""
        next_action = next_step("Waiting for reply", f"{supplier} has {quantity:,} ready but no truck to bring them yet{last}.",
                                active["received_at"] if active else None, active["action_id"] if active else None)
    stage = "confirm" if any(action["kind"] == "order" and action["status"] == "done"
                               and not json.loads(action.get("payload") or "{}").get("from_simulated_call") for action in related) else "order"
    return ({"id": f"offer-{offer['supplier_id']}-{component}", "name": f"Rush supply from {supplier}", "quantity": quantity,
             "steps": steps, "evidence": sources, "current_stage": stage, "next_step": next_action},
            {"name": "Rush supply", "supplier": supplier, "quantity": quantity, "cost": cost, "copiesAdded": copies,
             "refundsAvoided": avoided, "netIfTimely": round(avoided - cost, 2) if cost is not None else None,
             "nextConstraint": next_constraint, "editionId": target, "validUntil": valid,
             "latestUsefulAt": latest_release.isoformat(timespec="minutes") if latest_release else None,
             "releasedAt": released.isoformat(timespec="minutes") if released else None,
             "departBy": depart_cutoff.isoformat(timespec="minutes") if depart_cutoff else None,
             "requestedBy": requested_by.isoformat(timespec="minutes") if requested_by else None,
             "departForRequested": depart_requested.isoformat(timespec="minutes") if depart_requested else None,
             "modeledUnloadAt": modeled_unload.isoformat(timespec="seconds") if modeled_unload else None,
             "modeledUnloadAnchorAt": anchor.isoformat(timespec="seconds") if anchor else None,
             "modeledUnloadReplyId": anchor_reply_id, "modeledLateMinutes": late, "modeledUnloadBasis": modeled_basis,
             "task": task,
             "deliveryState": delivery or None, "needsRevisedTerms": delivery.get("needs_revised_terms", False),
             "departure": departure_state(delivery, now),
             "origin": origin,
             "timingState": timing, "evidence": sources, "assurance": None})  # no unsourced standby-premium example


def snapshot(checked_at: str | None, *, db_path=None, include_activity: bool = True) -> dict:
    from core import followup

    priority = rules.cfg("priority_product")
    deadline = rules.ts(rules.cfg("deadline"))
    cutoff = deadline - dt.timedelta(hours=rules.cfg("receiving_buffer_hours"))
    now = rules.now()

    action_log, decisions, activity, reviews = defaultdict(list), [], {}, {}
    if include_activity and actions.DB.is_file():
        with actions.read() as log:
            log.execute("BEGIN")
            action_log = linked_actions(log)
            decisions = [dict(row) for row in log.execute("""SELECT id,title,rationale,status,approver_name,next_check_at,due_at
                         FROM actions WHERE status IN ('proposed','escalated','scheduled') ORDER BY due_at, eur_protected DESC""")]
            activity = readable_activity(log)
            review_rows = log.execute("SELECT ref, MAX(ts) FROM events WHERE kind=? GROUP BY ref", (followup.REVIEW_KIND,))
            reviews = {ref: stamp for ref, ts in review_rows if ref and (stamp := rules.ts(ts))}
            for decision in decisions:
                action = actions._get(log, decision["id"])
                decision["cost"] = action["cost_eur"]
                decision["details"] = action["payload"].get("details")
                decision["messages"] = [{key: msg[key] for key in ("to_name", "subject", "body", "body_en")} for msg in action["messages"]]

    with closing(sqlite3.connect(f"{(db_path or rules.DB_PATH).resolve().as_uri()}?mode=ro", uri=True)) as conn:
        picture, coverage = rules.launch_state(conn)
        bom = {cid: row["per_copy"] for cid, row in coverage.items()}
        editions, allocations = forecast(picture, coverage, bom, priority)
        refund_per_copy = rules.cfg("missed_copy_cost_eur")
        names = dict(conn.execute("SELECT product_id,name FROM products"))
        for edition, row in editions.items():
            name = names.get(edition, edition)
            row["name"] = re.split(r"\s+[–—]\s+", name)[-1].split(" (")[0]
            row["refundPerCopy"] = refund_per_copy.get(edition, 0)
            row["refundsAtRisk"] = row["short"] * row["refundPerCopy"]
        supplier_rows = conn.execute("SELECT supplier_id,name,contact_email FROM suppliers").fetchall()
        suppliers = {sid: name for sid, name, _ in supplier_rows}
        contacts = {sid: address.casefold() for sid, _, address in supplier_rows if address}
        orders = defaultdict(list)
        for material, supplier, po in conn.execute(
                "SELECT DISTINCT material_id,supplier_id,po_number FROM purchase_orders ORDER BY supplier_id,po_number"):
            orders[material].append({"supplierId": supplier, "poNumber": po})
        places = {code: {"name": name, "lat": lat, "lon": lon} for code, name, lat, lon in
                  conn.execute("SELECT locode,name,lat,lon FROM ports") if coordinates(lat, lon)}
        site = rules.setting("site")
        port = rules.cfg("site_port")
        if site not in places and port in places:
            places[site] = {**places[port], "name": f"{rules.cfg('site_name')} area"}
        shipments = rules.inbound(conn, coverage)
        received = {"delivered", "arrived_dc"}
        shipments.sort(key=lambda s: (s["status"] in received, not s["planned_for_launch"], s["arrival_at_dc"] is None,
                                      s["arrival_at_dc"] or "", s["shipment_id"]))
        offers = defaultdict(list)
        for offer in rules.facts(conn, "offer"):
            if offer.get("qty", 0) and offer["qty"] > 0 and offer.get("material_id") in coverage and offer.get("supplier_id"):
                offers[offer["material_id"]].append(offer)

        components = []
        for cid, stock in coverage.items():
            name = rules.plain(stock["description"], "Part")
            rows = list(allocations[cid].values())
            status = "verify" if stock["suspect_master_data"] else "short" if stock["gap"] < 0 else "covered"
            gap_actions = action_log[f"gap-{cid}"]
            route_options = []
            for offer in offers[cid] if status == "short" else ():
                finding = f"offer-{offer['supplier_id']}-{cid}"
                contact = contacts.get(offer["supplier_id"])
                related = action_log[finding] + [action for action in gap_actions if contact and any(
                    (message["to_address"] or "").casefold() == contact for message in action["messages"])]
                alternative, _ = forecast(picture, coverage, bom, priority, {cid: int(offer["qty"])})
                route_options.append(recovery(conn, offer, suppliers, related, alternative, editions, cutoff, now))
            route_options.sort(key=lambda pair: (pair[1]["timingState"] != "open", -pair[1]["refundsAvoided"], -pair[1]["copiesAdded"]))
            option = route_options[0][1] if route_options else None
            if option and option["nextConstraint"] in coverage:
                option["nextConstraint"] = rules.plain(coverage[option["nextConstraint"]]["description"])
            inbound = [s for s in shipments if cid in s["materials"]]
            shipment_routes = {s["shipment_id"]: shipment_route(s, suppliers, action_log[f"ship-{s['shipment_id']}"], places, now)
                               for s in inbound}
            inventory = max(0, stock["usable_now"])
            gap_next = action_next_step(gap_actions)
            if status == "verify":
                stage, stock_next = "receive", action_next_step(action_log[f"master-{cid}"]) or next_step("Stuck", f"Check how {rules.cfg('site_name')} counts this part.")
            elif status == "short":
                stage, stock_next = "order", gap_next or next_step("Stuck", "Find more usable stock before packing.")
            elif rows and all(picture[row["editionId"]]["packed"] >= picture[row["editionId"]]["demand"] for row in rows):
                stage, stock_next = "dispatch", next_step("Confirmed", "The boxes are packed and ready for dispatch.")
            else:
                stage, stock_next = "pack", next_step("Confirmed", "Use the available stock to pack boxes.")
            stock_route = {"id": f"stock-{cid}", "name": f"Stock in {rules.cfg('site_name')}", "quantity": None if status == "verify" else inventory,
                           "steps": [step("Warehouse stock", "unknown" if status == "verify" else "confirmed",
                                          "Check whether this item is stocked separately." if status == "verify" else "Usable quantity after receipt corrections and quality holds.")],
                           "evidence": [evidence(rules.cfg("sources")["paths"]["stock"], "Stock snapshot adjusted for estimated consumption since the snapshot.")],
                           "current_stage": stage, "next_step": stock_next}
            launch_shipment = next((shipment_routes[s["shipment_id"]] for s in inbound if s["status"] not in received
                                    and not s["quantity_review"] and (s["planned_for_launch"] or s["status"] == "arrived_port")), None)
            review_route = next((shipment_routes[s["shipment_id"]] for s in inbound if s["quantity_review"]), None)
            viable_offer = next((route for route, scenario in route_options if scenario["timingState"] != "expired"), None)
            primary_route = (viable_offer or launch_shipment or review_route or stock_route) if status == "short" else stock_route
            routes = [route for route, _ in route_options] + list(shipment_routes.values()) + [stock_route]
            latest = latest_summary(gap_actions)
            components.append({"id": cid, "name": name, "status": status, "usable": inventory, "required": stock["need_total"],
                               "shared": len(rows) > 1, "allocations": rows, "purchaseOrders": orders[cid],
                               "routes": routes, "option": option, "includedIn": stock.get("included_in"),
                               "adjustments": [{"detail": adj["detail"], "evidence": adj.get("evidence", [])} for adj in stock["adjustments"]],
                               "latestActivity": latest["summary_en"] if latest else None, "default_route_id": primary_route["id"],
                               "current_stage": primary_route["current_stage"],
                               "next_step": gap_next if gap_next and gap_next["state"] == "Needs you" and primary_route is not review_route
                                            and not primary_route["next_step"].get("reply_id") else primary_route["next_step"]})

    for part in components:  # say what Dockside does next and when (only when follow-ups are switched on)
        last_useful = followup.deadline_for(part, cutoff)
        default = next((route for route in part["routes"] if route["id"] == part["default_route_id"]), None)
        for item in filter(None, [part, default]):
            current = item["next_step"]
            extra = followup.promise(part["id"], current, reviews, now, last_useful) if current and "base_text" not in current else ""
            if extra:
                item["next_step"] = dict(current, text=f"{current['text']} {extra}", base_text=current["text"])
        if default and part["next_step"] is not default["next_step"] and part["next_step"].get("base_text") == default["next_step"].get("base_text"):
            default["next_step"] = part["next_step"]
    ordered = sorted(editions, key=lambda edition: edition != priority)
    priority_constraints = editions.get(priority, {}).get("constraintIds", [])
    focus = next(iter(priority_constraints), next((item["id"] for item in components if item["status"] == "short"), None))
    by_component = {part["id"]: part for part in components}
    for part in components:
        if part["includedIn"]:
            parent = by_component[part["includedIn"]["componentId"]]
            part.update({key: parent[key] for key in ("default_route_id", "current_stage", "next_step")})
    ranked = sorted((part for part in components if not part["includedIn"]), key=lambda part: (
        part["id"] not in priority_constraints, part["next_step"]["state"] != "Needs you",
        part["status"] != "short", part["default_route_id"].startswith("stock-"),
        -(part["required"] - part["usable"])))
    now_next = []
    seen_actions = set()
    for part in ranked:
        aid = part["next_step"]["action_id"]
        if aid and aid in seen_actions:
            continue
        now_next.append({"component_id": part["id"], "next_step": part["next_step"]})
        seen_actions.add(aid)
        if len(now_next) == 3:
            break
    return {"now": now.isoformat(timespec="seconds"), "deadline": deadline.isoformat(), "cutoff": cutoff.isoformat(),
            "checkedAt": checked_at, "siteName": rules.cfg("site_name"), "display": rules.CONFIG.get("display", {}).get("value", {}), "priorityId": priority, "focusComponentId": focus, "editions": [editions[key] for key in ordered],
            "components": components, "now_next": now_next, "decisions": decisions, "activity": activity,
            "notes": ["The dispatch estimate counts modeled packed stock, usable parts on site and remaining packing capacity.",
                      "The recovery estimate assumes the parts arrive usable by the receiving cutoff and the line can pack them in time.",
                      "The receiving cutoff is a planning assumption."]}
