"""Evidence-backed launch picture and conditional supply recovery scenarios."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
from collections import defaultdict
from contextlib import closing

from core import actions, customer, rules, voice


def forecast(picture: dict, coverage: dict, bom: dict, priority: str, extra: dict | None = None) -> tuple[dict, dict]:
    """Reserve shared stock in priority order, then cap dispatch by every required part and packing capacity."""
    extra = extra or {}
    allocations, parts_by_edition = {}, defaultdict(dict)
    order = sorted(picture, key=lambda edition: edition != priority)
    for component, row in coverage.items():
        remaining = max(0, row["usable_now"]) + extra.get(component, 0)
        allocations[component] = {}
        for edition in order:
            per_copy = bom[component].get(edition)
            if per_copy is None:
                continue
            required = math.ceil(picture[edition]["todo_now"] * per_copy)
            allocated = min(required, remaining)
            remaining -= allocated
            allocations[component][edition] = {"editionId": edition, "required": required, "allocated": allocated, "perCopy": per_copy}
            if not row["suspect_master_data"]:
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
            return next_step("Waiting for reply", text, message["sent_at"], action["id"]) | {"person": who}
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


def readable_activity(log, limit: int = 10) -> dict:
    """Recent work in plain English: who was asked what, what they answered, what changed.
    The raw event log has e-mail addresses and foreign-language subjects; those stay out."""
    rows = log.execute("""SELECT e.ts, e.kind, e.ref, e.text, a.title FROM events e LEFT JOIN actions a ON a.id = e.ref
                          WHERE e.kind IN ('email_sent','approval_email_sent','reply','action_auto','action_approved','action_done','data_drop','docs_read')
                          ORDER BY e.ts DESC, e.rowid DESC LIMIT 60""").fetchall()
    replies = log.execute("SELECT action_id, from_name, received_at, summary_en FROM replies ORDER BY received_at").fetchall()
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
            reply = next((r for r in reversed(replies) if r[0] == ref and r[2] <= ts), None) if ref else None
            who = (reply[1] if reply else text.split(" replied", 1)[0]) or "Supplier"
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
             (staff, "message to staff", "messages to staff"), (calls, "simulated call", "simulated calls")]
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


def embedded_components(conn: sqlite3.Connection, coverage: dict) -> dict:
    """Keep supplier-fitted items under their documented parent in the bill of materials."""
    findings = defaultdict(dict)
    for component, note, supplier, folder in conn.execute("""SELECT sc.component_id,sc.note,s.name,s.folder
        FROM supplier_components sc JOIN suppliers s USING(supplier_id) WHERE sc.note LIKE '%fitted%into%'"""):
        match = re.search(r"fitted by supplier into\s+([\w-]+)", note, re.IGNORECASE)
        if match and match[1] in coverage and component in coverage and coverage[component]["suspect_master_data"]:
            findings[component][match[1]] = {"componentId": match[1], "supplier": supplier,
                                            "evidence": evidence(customer.supplier_path(rules.cfg("sources"), folder, "supplier_components"), note.split("|", 1)[0].strip())}
    return {component: next(iter(parents.values())) for component, parents in findings.items() if len(parents) == 1}


def linked_actions(conn: sqlite3.Connection) -> dict:
    if conn.in_transaction:
        conn.commit()
    voice.init_schema(conn)
    conn.execute("BEGIN")
    action_rows = {row["id"]: dict(row) | {"messages": [], "replies": []} for row in conn.execute(
        "SELECT id, finding_id, title, kind, status, updated_at, next_check_at, payload FROM actions")}
    call_rows = {row["msg_id"]: dict(row) for row in conn.execute(
        """SELECT v.*, r.summary_en, r.received_at FROM voice_calls v LEFT JOIN replies r ON r.id=v.reply_id""")}
    for message in conn.execute("SELECT id, action_id, channel, to_name, to_address, status, sent_at, provider_id, body FROM messages"):
        if action := action_rows.get(message["action_id"]):
            action["messages"].append(dict(message) | {"call": call_rows.get(message["id"])})
    parent_actions = {}
    for reply in conn.execute("""SELECT id, msg_id, action_id, from_name, summary_en, received_at, handled, simulated
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


ACCEPTED = re.compile(r"\b(acknowledg\w*|accept\w*|treat\w*\b.{0,60}\bas the\b.{0,40}\border|released the goods)", re.I)
REFUSED = re.compile(r"\b(not|no longer|cannot|can't|won't|declin\w*|reject\w*)\b.{0,20}\b(acknowledg|accept|treat|confirm\w* the order)", re.I)


def _clock(moment: dt.datetime | None) -> str:
    return f"{moment.astimezone(rules.timezone()):%a %H:%M}" if moment else ""


def _requested_arrival(action: dict, cutoff: dt.datetime) -> dt.datetime | None:
    """The arrival the order or unload-slot request asked for: typed terms first, else the window written in the request."""
    payload = json.loads(action.get("payload") or "{}")
    typed = rules.ts(((payload.get("commitment") or {}).get("terms") or payload.get("commitment") or {}).get("deliver_by"))
    if typed:
        return typed
    for message in action.get("messages", []):
        window = re.search(r"\b(\d{1,2}):(\d{2})\s*(?:to|until|bis|–|-)\s*(\d{1,2}):(\d{2})", message.get("body") or "")
        if window:
            day = cutoff.astimezone(rules.timezone())
            return day.replace(hour=int(window[3]), minute=int(window[4]), second=0, microsecond=0)
    return None


def offer_steps(related: list[dict], supplier: str, quantity: int, offer: dict, expired: bool, cutoff: dt.datetime):
    """One row per thing that must be true before the goods count: each with its own state and the latest evidence."""
    site = rules.cfg("site_name")
    replies = sorted((r for action in related for r in action["replies"] if not r.get("simulated")),
                     key=lambda r: r["received_at"] or "")
    done = [a for a in sorted(related, key=lambda a: a.get("updated_at") or "")
            if a["status"] in {"sent", "done", "auto", "delivered"}]

    def latest_from(action: dict) -> dict | None:
        when = rules.ts(action.get("updated_at"))
        names = {m.get("to_name") for m in action.get("messages", []) if m.get("to_name")}
        found = [r for r in replies if r.get("summary_en") and (r.get("action_id") == action["id"] or r.get("from_name") in names)
                 and (not when or (rules.ts(r.get("received_at")) or when) >= when)]
        return found[-1] if found else None

    def said(reply: dict | None) -> str:
        if not reply:
            return ""
        return f" Latest from {reply['from_name']} ({_clock(rules.ts(reply['received_at']))}): {_first_sentence(reply['summary_en'])}"

    order = next((a for a in reversed(done) if a["kind"] == "order"), None)
    slot = next((a for a in reversed(done) if a["kind"] == "dock_slot"), None)
    truck = next((a for a in reversed(done) if a["kind"] == "book_truck"), None)
    requested_by = next(filter(None, (_requested_arrival(a, cutoff) for a in (order, slot) if a)), None)
    steps = [step("Supplier stock", "confirmed", f"{supplier} offered {quantity:,} units.")]
    if order:
        reply, sent = latest_from(order), _clock(rules.ts(order.get("updated_at")))
        accepted = bool(reply and ACCEPTED.search(reply["summary_en"]) and not REFUSED.search(reply["summary_en"]))
        steps.append(step("Formal order", "confirmed" if accepted else "waiting",
                          (f"Sent {sent}; {reply['from_name']} accepted it {_clock(rules.ts(reply['received_at']))}." if accepted
                           else f"Sent {sent}; waiting for {supplier} to confirm it.") + ("" if accepted else said(reply))))
    else:
        steps.append(step("Formal order", "pending", "Release the order once transport and receiving conditions are met."))
    need = f" Needed by {_clock(requested_by)}, at the latest {_clock(cutoff)}." if requested_by else f" Needed by {_clock(cutoff)}."
    carrier = (offer.get("transport") or "").split(",")[0].strip() or "the carrier"
    if truck:
        steps.append(step("Carrier booking", "waiting", f"Truck requested {_clock(rules.ts(truck.get('updated_at')))}; no booking reference yet." + said(latest_from(truck))))
    else:
        steps.append(step("Carrier booking", "waiting" if order else "pending",
                          f"{supplier} is booking {carrier}; no booking reference yet." if order else f"{carrier} still has to be booked."))
    steps.append(step("Arrival time", "waiting" if order else "pending", "No confirmed arrival time yet." + need))
    if slot:
        window = re.search(r"\b\d{1,2}:\d{2}\s*(?:to|until|bis|–|-)\s*\d{1,2}:\d{2}", " ".join(m.get("body") or "" for m in slot.get("messages", [])))
        who = next((m.get("to_name") for m in slot.get("messages", []) if m.get("to_name")), "the site")
        who = re.split(r"\s+(?:and|\()\s*", who, maxsplit=1)[0]
        steps.append(step("Unload slot", "waiting", f"Requested{' ' + window[0].replace(' to ', '–') if window else ''} from {who} "
                          f"{_clock(rules.ts(slot.get('updated_at')))}; not confirmed yet."))
    else:
        steps.append(step("Unload slot", "pending", f"Dockside requests the unload slot with {site}; the site lead can move or stop it."))
    people = rules.cfg("people")
    asked = {}
    for action in sorted(related, key=lambda a: a.get("updated_at") or "", reverse=True):  # newest question per person wins
        if action["kind"] in ("ask", "escalate") and action["status"] in {"sent", "auto", "delivered"}:
            for message in action.get("messages", []):
                if message.get("channel") == "discord" and message.get("status") == "delivered" and not message_answered(message, action["replies"]):
                    step_ = internal_followup(action, message)
                    person = people.get(step_.get("person")) or step_.get("person")  # a role name means its holder
                    if person and person not in asked:
                        question = step_["text"].split(": ", 1)[-1]
                        asked[person] = step(f"Answer from {person}", "waiting", question[:1].upper() + question[1:] +
                                             f" Asked {_clock(rules.ts(message.get('sent_at')))}.")
    unique = steps + list(asked.values())
    unique.append(step("Receiving", "blocked" if expired else "pending", "Add to usable stock after receipt and inspection."))
    return unique, requested_by


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
    # Once the order left in time, the release window no longer matters; only the receiving cutoff does.
    released = min((rules.ts(a.get("updated_at")) for a in related if a["kind"] == "order" and a["status"] in {"sent", "done"}
                    and rules.ts(a.get("updated_at"))), default=None)
    released_in_time = bool(released and (not latest_release or released <= latest_release))
    expired = now >= cutoff or (not released_in_time and ((latest_release and now > latest_release) or (valid_day and now.date() > valid_day)))
    timing = "expired" if expired else "released" if released_in_time else "open" if latest_release else "unknown"
    active = latest_summary(related)
    priority = rules.cfg("priority_product")
    accounting = rules.cfg("missed_copy_cost_eur")
    deltas = {edition: row["projected"] - baseline[edition]["projected"] for edition, row in alt.items()}
    target = max(baseline, key=lambda edition: (deltas[edition] * accounting.get(edition, 0), deltas[edition], edition == priority))
    copies = deltas[target]
    avoided = round(sum(delta * accounting.get(edition, 0) for edition, delta in deltas.items()), 2)
    sources = [evidence(offer["_path"].rsplit("/", 1)[-1], offer.get("summary") or "Supplier offer")]
    if price_source:
        sources.append(evidence(price_source.rsplit("/", 1)[-1], "The offer points to Annex A; estimate uses the latest EUR purchase-order unit price."))
    if active:
        sources.insert(0, evidence(f"Supplier reply from {active['from_name']}", active["summary_en"]))
    steps, requested_by = offer_steps(related, supplier, quantity, offer, expired, cutoff)
    constraints = alt[target]["constraintIds"]
    next_constraint = constraints[0] if constraints else "Packing capacity" if alt[target]["capacityLimited"] else None
    logged = action_next_step(related, supplier)
    if expired:
        next_action = next_step("Stuck", "The rush order window has passed.")
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
             "releasedAt": released.isoformat(timespec="minutes") if released_in_time else None,
             "departBy": (cutoff - dt.timedelta(hours=drive)).isoformat(timespec="minutes") if drive is not None else None,
             "requestedBy": requested_by.isoformat(timespec="minutes") if requested_by else None,
             "departForRequested": (requested_by - dt.timedelta(hours=drive)).isoformat(timespec="minutes") if requested_by and drive is not None else None,
             "origin": (re.search(r"\b([A-ZÀ-Ž][\wÀ-ž]+)\s*[–-]\s*[A-ZÀ-Ž]", offer.get("transport") or "") or [None, None])[1],
             "timingState": timing, "evidence": sources,
             "assurance": {"examplePremium": rules.cfg("assurance_example_eur"), "coveredStep": "Transport",
                           "sharedSteps": ["Unload slot", "Formal order", "Receiving"]} if drive and avoided > 0 else None})


def snapshot(checked_at: str | None, *, db_path=None, include_activity: bool = True) -> dict:
    priority = rules.cfg("priority_product")
    deadline = rules.ts(rules.cfg("deadline"))
    cutoff = deadline - dt.timedelta(hours=rules.cfg("receiving_buffer_hours"))
    now = rules.now()

    action_log, decisions, activity = defaultdict(list), [], {}
    if include_activity:
        with closing(actions.con()) as log:
            action_log = linked_actions(log)
            decisions = [dict(row) for row in log.execute("""SELECT id,title,rationale,status,approver_name,next_check_at,due_at
                         FROM actions WHERE status IN ('proposed','escalated','scheduled') ORDER BY due_at, eur_protected DESC""")]
            activity = readable_activity(log)
        for decision in decisions:
            action = actions.get(decision["id"])
            decision["cost"] = action["cost_eur"]
            decision["details"] = action["payload"].get("details")
            decision["messages"] = [{key: msg[key] for key in ("to_name", "subject", "body", "body_en")} for msg in action["messages"]]

    with closing(sqlite3.connect(f"{(db_path or rules.DB_PATH).resolve().as_uri()}?mode=ro", uri=True)) as conn:
        picture = rules.launch_picture(conn)
        coverage = rules.component_coverage(conn, picture)
        bom = defaultdict(dict)
        for edition, component, units in conn.execute("SELECT parent_id,child_id,qty_per FROM bom WHERE qty_per > 0"):
            if edition in picture:
                bom[component][edition] = units
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
        embedded = embedded_components(conn, coverage)
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
                               "routes": routes, "option": option, "includedIn": embedded.get(cid),
                               "adjustments": [{"detail": adj["detail"], "evidence": adj.get("evidence", [])} for adj in stock["adjustments"]],
                               "latestActivity": latest["summary_en"] if latest else None, "default_route_id": primary_route["id"],
                               "current_stage": primary_route["current_stage"],
                               "next_step": gap_next if gap_next and gap_next["state"] == "Needs you" and primary_route is not review_route else primary_route["next_step"]})

    from core import followup
    reviews = followup.last_reviews() if include_activity else {}
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
                      "The receiving cutoff is a planning assumption. The standby premium is an example you can change."]}
