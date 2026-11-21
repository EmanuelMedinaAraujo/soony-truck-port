"""Evidence-backed launch picture and conditional supply recovery scenarios."""
from __future__ import annotations

import datetime as dt
import math
import re
import sqlite3
from collections import defaultdict
from contextlib import closing

from core import actions, inbox, rules


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


def shipment_route(shipment: dict, suppliers: dict, replies: list[dict], places: dict) -> dict:
    flags = shipment["flags"]
    ref = shipment["reference"] or shipment["shipment_id"]
    arrival = shipment["arrival_at_dc"]
    received = shipment["status"] in ("delivered", "arrived_dc")
    at_port = shipment["status"] == "arrived_port"
    hold = next((flag for flag in flags if flag.startswith("supplier hold")), None)
    moving = shipment["status"] in ("in_transit", "in_delivery", "arrived_port", "delivered", "arrived_dc")
    uncertain = next((flag for flag in flags if "truck ban" in flag or "no carrier update" in flag), None)
    latest_reply = next((row for row in replies if row["summary_en"]), None)
    transport = "blocked" if hold else "pending" if uncertain or not moving else "confirmed"
    receiving = "confirmed" if received else "blocked" if shipment["makes_cutoff"] is False else "pending"
    if received:
        receiving_note = "The shipment record shows this load arrived. Usable stock is counted from inventory records."
    elif arrival and shipment["makes_cutoff"] is False:
        receiving_note = f"Tracker estimate {arrival[5:16].replace('T', ' ')} is after the receiving cutoff."
    elif arrival:
        receiving_note = f"Tracker estimate {arrival[5:16].replace('T', ' ')}. Receiving is still open."
    else:
        receiving_note = "Arrival and a receiving slot still need confirmation."
    source = suppliers.get(shipment["supplier_id"], "Supplier shipment")
    mode = {"sea": "Container", "air": "Air freight", "road": "Truck"}.get(shipment["mode"], "Shipment")
    steps = [step("Supplier release", "blocked" if hold else "confirmed" if moving else "pending",
                  hold or ("Dispatch appears in the shipment record." if moving else "The tracker shows a booking. Dispatch has yet to be confirmed."))]
    if at_port:
        steps.extend([step("Port arrival", "confirmed", f"The terminal shows {ref} available for pickup."),
                      step("Truck pickup", "pending", "A carrier pickup still needs confirmation.")])
    else:
        steps.append(step("Transport", transport, hold or uncertain or "The shipment tracker records this leg."))
    steps.append(step("Hamburg receiving", receiving, receiving_note))
    sources = [evidence(path.rsplit("/", 1)[-1], f"Shipment {ref}") for path in shipment["evidence"]]
    if latest_reply:
        sources.insert(0, evidence(f"Supplier reply from {latest_reply['from_name']}", latest_reply["summary_en"]))
    return {"id": shipment["shipment_id"], "name": f"{mode} from {source}" if source != "Supplier shipment" else mode,
            "quantity": shipment["quantity"] if len(shipment["materials"]) == 1 else None,
            "steps": steps, "evidence": sources, "tracking": shipment_tracking(shipment, places, hold)}


def embedded_components(conn: sqlite3.Connection, coverage: dict) -> dict:
    """Keep supplier-fitted items under their documented parent in the bill of materials."""
    findings = defaultdict(dict)
    for component, note, supplier, folder in conn.execute("""SELECT sc.component_id,sc.note,s.name,s.folder
        FROM supplier_components sc JOIN suppliers s USING(supplier_id) WHERE sc.note LIKE '%fitted%into%'"""):
        match = re.search(r"fitted by supplier into\s+([\w-]+)", note, re.IGNORECASE)
        if match and match[1] in coverage and component in coverage and coverage[component]["suspect_master_data"]:
            findings[component][match[1]] = {"componentId": match[1], "supplier": supplier,
                                            "evidence": evidence(f"{folder}/components.csv", note.split("|", 1)[0].strip())}
    return {component: next(iter(parents.values())) for component, parents in findings.items() if len(parents) == 1}


def linked_replies(conn: sqlite3.Connection) -> dict:
    conn.executescript(inbox.REPLY_SCHEMA)
    rows = [dict(row) for row in conn.execute("""SELECT r.id, r.from_name, r.summary_en, r.received_at, a.finding_id
                       FROM replies r JOIN actions a ON a.id=r.action_id ORDER BY r.received_at DESC""")]
    by_id = {row["id"]: row for row in rows}
    grouped = defaultdict(list)
    for row in rows:
        finding = row["finding_id"]
        visited = set()
        while finding.startswith("reply-") and finding not in visited:
            visited.add(finding)
            parent = by_id.get(finding.removeprefix("reply-"))
            if parent is None:
                break
            finding = parent["finding_id"]
        grouped[finding].append(row)
    return grouped


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


def recovery(conn, offer, suppliers, replies, alt, baseline, cutoff, now):
    component = offer["material_id"]
    quantity = int(offer["qty"])
    supplier = suppliers.get(offer["supplier_id"], "Supplier")
    unit_price, price_source = offer_price(conn, offer)
    cost = round(unit_price * quantity, 2) if unit_price is not None else None
    load, drive = hours_in(offer.get("lead_time_note")), hours_in(offer.get("transport"))
    latest = cutoff - dt.timedelta(hours=load + drive) if load is not None and drive is not None else None
    valid = offer.get("valid_until")
    try:
        valid_day = dt.date.fromisoformat(valid[:10]) if valid else None
    except ValueError:
        valid_day = None
    expired = now >= cutoff or (latest and now > latest) or (valid_day and now.date() > valid_day)
    timing = "expired" if expired else "open" if latest else "unknown"
    active = next((reply for reply in replies if reply["summary_en"]), None)
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
    steps = [step("Supplier stock", "confirmed", f"{supplier} offered {quantity:,} units."),
             step("Transport", "pending", (offer.get("transport") or "Carrier and travel time still need confirmation.")
                  + " Verify vehicle capacity and a legal route."),
             step("Dock slot", "pending", "Agree a receiving slot with the site lead."),
             step("Formal order", "pending", "Release the commitment once transport and receiving conditions are met."),
             step("Hamburg receiving", "blocked" if expired else "pending", "Add to usable stock after receipt and inspection.")]
    constraints = alt[target]["constraintIds"]
    next_constraint = constraints[0] if constraints else "Packing capacity" if alt[target]["capacityLimited"] else None
    return ({"id": f"offer-{offer['supplier_id']}-{component}", "name": f"Rush supply from {supplier}", "quantity": quantity,
             "steps": steps, "evidence": sources},
            {"name": "Rush supply", "supplier": supplier, "quantity": quantity, "cost": cost, "copiesAdded": copies,
             "refundsAvoided": avoided, "netIfTimely": round(avoided - cost, 2) if cost is not None else None,
             "nextConstraint": next_constraint, "editionId": target, "validUntil": valid,
             "latestUsefulAt": latest.isoformat(timespec="minutes") if latest else None,
             "timingState": timing, "evidence": sources,
             "assurance": {"examplePremium": rules.cfg("assurance_example_eur"), "coveredStep": "Transport",
                           "sharedSteps": ["Dock slot", "Formal order", "Hamburg receiving"]} if drive and avoided > 0 else None})


def snapshot(checked_at: str | None) -> dict:
    priority = rules.cfg("priority_product")
    deadline = rules.ts(rules.cfg("deadline"))
    cutoff = deadline - dt.timedelta(hours=rules.cfg("receiving_buffer_hours"))
    now = rules.now()

    with closing(actions.con()) as log:
        replies = linked_replies(log)
        decisions = [dict(row) for row in log.execute("""SELECT id,title,rationale,status,approver_name,next_check_at,due_at
                     FROM actions WHERE status IN ('proposed','escalated','scheduled') ORDER BY due_at, eur_protected DESC""")]
        activity = [dict(row) for row in log.execute("""SELECT ts,text FROM events WHERE kind IN
                    ('email_sent','reply','action_auto','action_scheduled','action_approved','data_drop') ORDER BY ts DESC, rowid DESC LIMIT 8""")]
    for decision in decisions:
        action = actions.get(decision["id"])
        decision["cost"] = action["cost_eur"]
        decision["details"] = action["payload"].get("details")
        decision["messages"] = [{key: msg[key] for key in ("to_name", "subject", "body", "body_en")} for msg in action["messages"]]

    with closing(sqlite3.connect(f"file:{rules.DB_PATH}?mode=ro", uri=True)) as conn:
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
        suppliers = dict(conn.execute("SELECT supplier_id,name FROM suppliers"))
        places = {code: {"name": name, "lat": lat, "lon": lon} for code, name, lat, lon in
                  conn.execute("SELECT locode,name,lat,lon FROM ports") if coordinates(lat, lon)}
        site = rules.CONFIG["site"]
        if site not in places and site == "DC-HAM" and "DEHAM" in places:
            places[site] = {**places["DEHAM"], "name": "Hamburg area"}
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
            name = rules.plain(stock["description"], cid)
            rows = list(allocations[cid].values())
            status = "verify" if stock["suspect_master_data"] else "short" if stock["gap"] < 0 else "covered"
            route_options = []
            for offer in offers[cid] if status == "short" else ():
                finding = f"offer-{offer['supplier_id']}-{cid}"
                related = sorted(replies[finding] + replies[f"gap-{cid}"], key=lambda r: r["received_at"] or "", reverse=True)
                alternative, _ = forecast(picture, coverage, bom, priority, {cid: int(offer["qty"])})
                route_options.append(recovery(conn, offer, suppliers, related, alternative, editions, cutoff, now))
            route_options.sort(key=lambda pair: (pair[1]["timingState"] != "open", -pair[1]["refundsAvoided"], -pair[1]["copiesAdded"]))
            option = route_options[0][1] if route_options else None
            if option and option["nextConstraint"] in coverage:
                option["nextConstraint"] = rules.plain(coverage[option["nextConstraint"]]["description"])
            routes = [route for route, _ in route_options]
            routes.extend(shipment_route(s, suppliers, replies[f"ship-{s['shipment_id']}"], places) for s in shipments if cid in s["materials"])
            inventory = max(0, stock["usable_now"])
            routes.append({"id": f"stock-{cid}", "name": "Stock in Hamburg", "quantity": None if status == "verify" else inventory,
                           "steps": [step("Warehouse stock", "unknown" if status == "verify" else "confirmed",
                                          "Check whether this item is stocked separately." if status == "verify" else "Usable quantity after receipt corrections and quality holds.")],
                           "evidence": [evidence("stock_snapshot.csv", "Stock snapshot adjusted for estimated consumption since the snapshot.")]})
            components.append({"id": cid, "name": name, "status": status, "usable": inventory, "required": stock["need_total"],
                               "shared": len(rows) > 1, "allocations": rows, "routes": routes, "option": option, "includedIn": embedded.get(cid),
                               "adjustments": [{"detail": adj["detail"], "evidence": adj.get("evidence", [])} for adj in stock["adjustments"]],
                               "latestActivity": next((r["summary_en"] for r in replies[f"gap-{cid}"] if r["summary_en"]), None)})

    ordered = sorted(editions, key=lambda edition: edition != priority)
    priority_constraints = editions.get(priority, {}).get("constraintIds", [])
    focus = next(iter(priority_constraints), next((item["id"] for item in components if item["status"] == "short"), None))
    return {"now": now.isoformat(timespec="seconds"), "deadline": deadline.isoformat(), "cutoff": cutoff.isoformat(),
            "checkedAt": checked_at, "priorityId": priority, "focusComponentId": focus, "editions": [editions[key] for key in ordered],
            "components": components, "decisions": decisions, "activity": activity,
            "notes": ["The dispatch estimate counts modeled packed stock, usable parts in Hamburg and remaining packing capacity.",
                      "The recovery estimate assumes the parts arrive usable by the receiving cutoff and the line can pack them in time.",
                      "The receiving cutoff is a planning assumption. The standby premium is an example you can change."]}
