"""Dependencies of a component, grounded in the BOM and supplier records."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from core import rules


FITTED_INTO = re.compile(r"\bfitted (?:by (?:the )?supplier )?into\s+([\w-]+)", re.I)
SUB_SUPPLIER = re.compile(
    r"\b(?:our|their|its)\s+(?:[\w-]+\s+){0,5}supplier\s+"
    r"([A-Z][\w&' -]+?)(?=\s+(?:Co\b|Ltd\b|LLC\b|has\b|have\b|is\b)|[,(])",
    re.I,
)
RISK_TYPES = {"supplier_hold": "Supply hold", "other_risk": "Supply risk", "capacity_notice": "Capacity notice"}
RECEIVED = {"delivered", "arrived_dc", "received"}


TERMS = [
    (re.compile(r"\bDDP\s+([A-Z][\w-]+)"), r"delivery to \1 (DDP)"),
    (re.compile(r"\bDAP\s+([A-Z][\w-]+)"), r"delivery to \1 (DAP)"),
    (re.compile(r"\bEXW\s+([A-Z][\w-]+)"), r"pickup at \1 (EXW)"),
    (re.compile(r"\bFCA\s+([A-Z][\w-]+)"), r"handover to our carrier in \1 (FCA)"),
    (re.compile(r"\bcompleted PPAP\b"), "a completed quality approval (PPAP)"),
    (re.compile(r"\bULDs?\b"), "air cargo containers"),
    (re.compile(r"\bpcs\b"), "pieces"),
]


def plain(text: str | None) -> str | None:
    """Supplier jargon in plain words: delivery terms, 'Buyer', ULDs; tidy doubled full stops."""
    if not text:
        return text
    customer_name = rules.cfg("customer_name")
    text = re.sub(r"\bBuyer order\b", lambda _: f"{customer_name}'s order", text)
    text = re.sub(r"\b[Bb]uyer\b", lambda _: customer_name, text)
    for pattern, replacement in TERMS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"\.{2,}", ".", text)
    return re.sub(r"\s+", " ", text).strip()


def quantity_label(quantity):
    return f"needs {quantity:g} each" if quantity is not None else "uses"


def supplier_risks(conn, supplied_parts):
    """Scope notices to the source and part named in the document or its facts."""
    notices = defaultdict(list)
    for path, kind, payload, supplier_id, doc_type, body in conn.execute("""
        SELECT f.path, f.fact_type, f.payload, d.supplier_id, d.doc_type, d.text
        FROM doc_facts f JOIN documents d USING (path)
        WHERE f.fact_type IN ('supplier_hold', 'other_risk', 'capacity_notice')
        ORDER BY f.path, f.fact_type, f.payload
    """):
        fact = json.loads(payload)
        supplier = fact.get("supplier_id") or supplier_id
        if supplier in supplied_parts:
            notices[supplier, path].append((kind, fact, doc_type, body or ""))

    for (supplier, path), facts in notices.items():
        scoped_parts = {fact["material_id"] for _, fact, _, _ in facts if fact.get("material_id")}
        relevant = [row for row in facts if row[1].get("material_id") in supplied_parts[supplier]
                    or (not row[1].get("material_id") and (
                        bool(scoped_parts & supplied_parts[supplier]) if scoped_parts else row[2] != "contracts"
                    ))]
        if not relevant:
            continue
        kind, fact, _, body = next((row for row in relevant if row[0] == "supplier_hold"), relevant[0])
        detail = fact.get("reason") or fact.get("summary") or fact.get("capacity_note") or fact.get("quote")
        summaries = dict.fromkeys(
            summary for _, related, _, _ in relevant
            if (summary := related.get("summary")) and summary != detail
        )
        if detail and summaries and not re.search(r"[.!?]$", detail.strip()):
            detail = None  # a terse note like "carrier capacity; 2 ULDs offloaded" repeats the summary
        if condition := fact.get("unblock_condition"):
            summaries[f"To release it: {condition}"] = None
        upstream = SUB_SUPPLIER.search(body)
        export_licence = re.search(r"export.{0,25}licen[cs]|licen[cs].{0,25}export", detail or "", re.I)
        text = " ".join(filter(None, [detail, *summaries])).lower()
        title = ("Export licence" if export_licence else
                 "Release pending" if re.search(r"ppap|written release|qualif", text) else
                 "Downtime" if re.search(r"downtime|maintenance|shutdown|przest", text) else
                 "Failed inspection" if re.search(r"inspection|aql|reject", text) else
                 "Short shipment" if re.search(r"offload|uld|short ship|not shipped|balance", text) else
                 "Terms dispute" if re.search(r"\bexw\b|\bddp\b|incoterm|terms of delivery", text) else RISK_TYPES[kind])
        detail = plain(" ".join(f"{part.rstrip('.')}." for part in filter(None, [detail, *summaries])))
        windows = [(row[1].get("from_ts"), row[1].get("to_ts")) for row in relevant if row[1].get("from_ts") or row[1].get("to_ts")]
        yield {"supplier": supplier, "path": path, "title": title, "upstream": upstream[1].strip() if upstream else None,
               "detail": detail or None, "scoped": scoped_parts, "window": windows[0] if windows else None,
               "quote": None if detail else fact.get("quote"), "status": "blocked" if kind == "supplier_hold" else "at_risk"}


def shipment_reasons(shipment: dict, carrier: str | None) -> list[tuple[str, str]]:
    """(short card line, full sentence) per problem, most serious first; a single ('', ...) entry when all is well."""
    if claims := shipment.get("quantity_review"):
        return [(0, "Hold for review", rules.shipment_review_detail(claims))]
    found = []
    for flag in shipment["flags"]:
        if flag.startswith("supplier hold"):
            reason, _, condition = flag.removeprefix("supplier hold:").partition("->")
            found.append((0, "Held by the supplier", plain(f"Held by the supplier: {reason.strip()}." + (f" Released when: {condition.strip()}." if condition.strip() else ""))))
        elif "no truck booked" in flag:
            found.append((1, "Waiting at the port", f"Waiting at the port. No truck is booked to {rules.cfg('site_name')} yet."))
        elif "passed but still 'booked'" in flag:
            due = rules.ts(shipment["etd"])
            found.append((3, "Not picked up yet", f"Not picked up yet: pickup was due {due:%a %d %b} but the carrier hasn't confirmed it, so the arrival moves later." if due else "Not picked up yet."))
        elif "truck ban" in flag:
            found.append((4, "Truck ban", flag[:1].upper() + flag[1:] + "."))
        elif "no carrier update" in flag:
            hours = re.search(r"(\d+) h", flag)
            found.append((6, "No carrier update", f"No carrier update for {hours[1] if hours else 'many'} hours."))
        elif flag.startswith("short shipment"):
            found.append((5, "Short shipment", plain("Short shipment: " + flag.split(":", 1)[1].strip() + ".")))
        elif flag.startswith("ETA assumes"):
            days = re.search(r"standard is ([\d.]+) d", flag)
            found.append((7, "ETA too optimistic", f"The carrier's arrival date is too optimistic: this route usually takes {days[1] if days else 'longer'} days."))
        else:
            found.append((8, flag[:30], flag))
    if shipment["makes_cutoff"] is False:
        found.append((2, "Misses the cut-off", "Arrives after the packing cut-off, too late for launch boxes."))
    if shipment["status"] == "delayed":
        found.append((5, "Delayed", "The carrier reports a delay."))
    if not found:
        return [("", f"On its way with {carrier}." if carrier else "On its way.")]
    return [(short, text) for _, short, text in sorted(found)]


def part_graph(component_id: str, db_path: Path = rules.DB_PATH) -> dict | None:
    """Build the graph, or return None for a component absent from the master."""
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        components = dict(conn.execute("SELECT component_id, description FROM components"))
        if component_id not in components:
            return None

        products = dict(conn.execute("SELECT product_id, name FROM products"))
        bom_parents = defaultdict(dict)
        children = defaultdict(dict)
        for parent, child, qty in conn.execute("SELECT parent_id, child_id, qty_per FROM bom WHERE qty_per > 0"):
            bom_parents[child][parent] = qty
            if parent in components and child in components and parent != child:
                children[parent][child] = None

        sources = defaultdict(list)
        for material, supplier, name, status, share, primary, note in conn.execute("""
            SELECT sc.component_id, sc.supplier_id, s.name, s.status, sc.share, sc.is_primary, sc.note
            FROM supplier_components sc JOIN suppliers s USING (supplier_id)
            ORDER BY s.name, sc.component_id
        """):
            sources[material].append((supplier, name, status, share, primary))
            match = FITTED_INTO.search(note or "")
            if match and match[1] in components and material in components and match[1] != material:
                children[match[1]][material] = note.split("|", 1)[0].strip()

        parents = defaultdict(set)
        for parent, parts in children.items():
            for child in parts:
                parents[child].add(parent)

        evaluation = rules.evaluate(db_path)
        coverage = evaluation["coverage"]
        nodes, edges = {}, []

        def add_node(id, label, kind, status="ok", detail=None, **extra):
            nodes[id] = dict(id=id, label=label, kind=kind, status=status, detail=detail, **extra)

        def add_edge(source, target, label, **extra):
            edges.append(dict(source=source, target=target, label=label, **extra))

        def using_products(part):
            found, seen, pending = set(), set(), [part]
            while pending:
                child = pending.pop()
                if child in seen:
                    continue
                seen.add(child)
                found.update(bom_parents[child].keys() & products.keys())
                pending.extend(parents[child] - seen)
            return found

        def add_part(part, kind):
            row = coverage.get(part, {})
            gap = row.get("gap", 0)
            shortage = gap < 0 and not row.get("suspect_master_data")
            detail = f"{-gap:,} short on site." if shortage else None
            add_node(part, rules.plain(components[part], "Part"), kind,
                     "at_risk" if shortage else "ok", detail, code=part)

        def add_assembly(parent, child):
            fitted_note = children[parent][child]
            if fitted_note:
                add_edge(child, parent, "fitted into", detail=fitted_note)
            else:
                add_edge(parent, child, quantity_label(bom_parents[child].get(parent)))

        add_part(component_id, "part")
        upstream = sorted(parents[component_id])
        for parent in upstream:
            add_part(parent, "assembly")
            add_assembly(parent, component_id)
        subparts = sorted(children[component_id].keys() - set(upstream))
        for child in subparts:
            add_part(child, "subpart")
            add_assembly(component_id, child)

        # The finished products are not drawn: the selected part says what it goes into.
        def edition(product):
            name = re.sub(r"\s+\([^)]*\)$", "", products.get(product) or "")
            return name.split(" – ")[-1].split(" - ")[-1].strip() or "Finished product"
        uses = {}
        for product in sorted(using_products(component_id), key=lambda p: p != rules.cfg("priority_product")):
            via = next((parent for parent in upstream if product in using_products(parent)), component_id)
            uses[edition(product)] = bom_parents[via].get(product)
        if uses:
            amounts = set(uses.values())
            each = f", {next(iter(amounts)):g} each" if len(amounts) == 1 and None not in amounts else ""
            names = " and ".join(uses) if len(uses) <= 2 else "all editions"
            if each:
                nodes[component_id]["meta"] = f"For {names}{each}"
            else:
                nodes[component_id]["meta"] = "For " + ", ".join(f"{name} {qty:g}" for name, qty in uses.items() if qty)

        own = coverage.get(component_id, {})
        fitted_into = [parent for parent in upstream if children[parent][component_id]]
        holder = coverage.get(fitted_into[0], {}) if fitted_into else own
        short = holder.get("gap", 0) < 0 and not holder.get("suspect_master_data")
        nodes[component_id].update(status="ok", detail=None)
        verdict = (f"{-holder['gap']:,} short in {rules.cfg('site_name')}." if short else
                   f"Enough usable stock in {rules.cfg('site_name')} for the launch. The issues below don't affect it.")

        def calm(status):
            return status if short or status == "ok" else "info"

        supplied_parts = defaultdict(set)
        materials = [component_id, *subparts, *fitted_into]
        profiles = {}
        for folder, sid in conn.execute("SELECT folder, supplier_id FROM suppliers"):
            try:
                profiles[sid] = json.loads((rules.ROOT / "data" / "current" / "suppliers" / (folder or "") / "profile.json").read_text()).get("supplier", {})
            except (OSError, ValueError, AttributeError):
                profiles[sid] = {}
        for part in materials:
            for supplier, name, status, share, primary in sources[part]:
                supplied_parts[supplier].add(part)
                status = (status or "").casefold().strip()
                health = "blocked" if status in {"blocked", "suspended", "on hold", "inactive"} else (
                    "ok" if status == "active" else "at_risk"
                )
                qualification = profiles.get(supplier, {}).get("qualification") or {}
                valid = rules.ts(qualification.get("validUntil")) if qualification.get("validUntil") else None
                reasons = []
                if valid and valid < rules.now() and not qualification.get("extendedBy"):
                    reasons.append(f"Approval expired {valid:%d %b}".replace(" 0", " "))
                    health = "at_risk" if health == "ok" else health
                elif (qualification.get("status") or "").casefold() == "conditional":
                    reasons.append("Only conditionally approved")
                if status not in {"", "active"}:
                    reasons.append({"watch": "On the watch list", "at risk": "Flagged at risk"}.get(status, status.capitalize()))
                share_text = f"{share:.0%} of supply" if share is not None else ""
                is_primary = str(primary).lower() in {"y", "yes", "true", "1"}
                detail = ". ".join(filter(None, [*reasons, "Primary supplier" if is_primary else "", share_text]))
                meta = reasons[0] if reasons else ("Primary supplier" if is_primary else "Supplier")
                add_node(supplier, name or "Supplier", "supplier", calm(health), detail or None, code=supplier, meta=meta)
                add_edge(part, supplier, "made by", detail=share_text or None)

        merged = {}
        for risk in supplier_risks(conn, supplied_parts):
            key = (risk["supplier"], risk["upstream"] or risk["title"])
            if key in merged:  # the same downtime mentioned in two documents is one risk
                known = merged[key]
                known["detail"] = " ".join(dict.fromkeys(filter(None, [known["detail"], risk["detail"]])))
                known["window"] = known["window"] or risk["window"]
                known["paths"].append(risk["path"])
                known["scoped"] |= risk["scoped"]
                if risk["status"] == "blocked":
                    known["status"] = "blocked"
                continue
            merged[key] = {**risk, "paths": [risk["path"]]}

        def when(stamp):
            moment = rules.ts(stamp)
            if not moment:
                return None
            return f"{moment:%a %H:%M}" if abs((moment - rules.now()).days) < 6 else f"{moment:%d %b}".lstrip("0")

        for (supplier, _), risk in merged.items():
            rid = f"risk:{supplier}:{risk['paths'][0]}"
            title = risk["title"]
            window = risk["window"]
            label = risk["upstream"] or title
            if window and not risk["upstream"]:
                start, stop = when(window[0]), when(window[1])
                label = f"{title} {start} to {stop}" if start and stop else f"{title} until {stop}" if stop else f"{title} from {start}"
            meta = (title if risk["upstream"] else
                    "Stops supply until solved" if risk["status"] == "blocked" else
                    "Pauses production; ready stock can still ship" if window else "May delay new supply")
            if not short:
                meta = "No effect: enough stock"
            add_node(rid, label, "risk", calm(risk["status"]), risk["detail"], subtitle=title if risk["upstream"] else None,
                     meta=meta, evidence=risk["paths"][0], sources=[path.rsplit("/", 1)[-1] for path in risk["paths"]],
                     quote=risk["quote"])
            add_edge(supplier, rid, "supply risk")

        carrier_names = dict(conn.execute("SELECT carrier_id, name FROM carriers"))
        deadline = rules.ts(rules.cfg("deadline"))
        later = defaultdict(list)  # parent node -> shipments that arrive after launch (collapsed into one node)
        mode_names = {"sea": "Sea freight", "air": "Air freight", "road": "Truck", "rail": "Rail freight"}
        for shipment in sorted(evaluation["inbound"], key=lambda row: (row["arrival_at_dc"] or row["eta_erp"] or "", row["shipment_id"])):
            carrying = [part for part in materials if part in shipment["materials"]]
            if not carrying or shipment["status"] in RECEIVED:
                continue
            status = shipment["status"]
            held = status in {"held", "blocked", "on_hold", "cancelled"} or any(
                flag.startswith("supplier hold") for flag in shipment["flags"]
            )
            stuck = shipment["arrival_at_dc"] is None
            arrival = rules.ts(shipment["arrival_at_dc"] or shipment["eta_erp"])
            supplier = shipment["supplier_id"]
            parent = supplier if supplier in supplied_parts and supplied_parts[supplier].intersection(carrying) else carrying[0]
            if not stuck and arrival and arrival > deadline and not shipment["planned_for_launch"]:
                later[parent].append((arrival, shipment))
                continue
            reasons = shipment_reasons(shipment, carrier_names.get(shipment["carrier_id"]))
            problems = [short for short, _ in reasons if short]
            uncertain = bool(problems)
            shipment_id = shipment["shipment_id"]
            mode = mode_names.get(shipment["mode"], "Shipment")
            if stuck:
                label = f"{mode}, at the port" if status == "arrived_port" else f"{mode}, held"
            else:
                label = f"{mode}, {when(arrival.isoformat())}"
            add_node(shipment_id, label, "shipment", calm("blocked" if held or stuck else "at_risk" if uncertain else "ok"),
                     " ".join(text for _, text in reasons) or None, code=shipment_id, reference=shipment["reference"],
                     meta=(problems[0] if problems else "Arrives before the cut-off") if short or not problems
                     else "No effect: enough stock",
                     subtitle="Arrives before launch" if not stuck and shipment["makes_cutoff"] else "Inbound shipment")
            add_edge(parent, shipment_id, "held" if held else "stuck" if stuck else "on its way")
        for parent, rows in later.items():
            first, last = rows[0][0], rows[-1][0]
            span = first.strftime("%d %b").lstrip("0") + ("" if len(rows) == 1 else f" to {last.strftime('%d %b').lstrip('0')}")
            nid = f"later:{parent}"
            add_node(nid, f"{len(rows)} later {'delivery' if len(rows) == 1 else 'deliveries'}, {span}", "shipment", "info",
                     "They arrive after the last trucks leave for the launch, so they don't help it: " + ", ".join(r[1].get("reference") or r[1]["shipment_id"] for r in rows) + ".",
                     subtitle="After launch", meta="Too late for the launch")
            add_edge(parent, nid, "after launch")

    return {"nodes": list(nodes.values()), "edges": edges, "verdict": verdict, "short": short}


def annotate(graph: dict, component: dict | None) -> dict:
    """Add what Dockside is doing about each risk and delivery, so no card leaves the reader guessing."""
    if not component:
        return graph
    routes = component.get("routes", [])
    plan = next((r for r in routes if r["id"] == component.get("default_route_id")), None)
    plan_name = plan["name"][:1].lower() + plan["name"][1:] if plan else None
    by_route = {r["id"]: r for r in routes}
    suppliers = {n["id"]: n["label"] for n in graph["nodes"] if n["kind"] == "supplier"}
    supplier_of = {e["target"]: e["source"] for e in graph["edges"] if e["source"] in suppliers}
    step = component.get("next_step") or {}
    idle = f"Not pursued: Dockside's plan uses {plan_name} instead." if plan_name else None

    def route_doing(route: dict) -> str | None:
        if route is plan:
            return f"Dockside: {step['text']}" if step.get("text") else None
        route_step = route.get("next_step") or {}
        if route_step.get("state") == "After launch":
            return None
        return f"Dockside: {route_step['text']}" if route_step.get("action_id") else idle

    # A rush offer of ready stock is a delivery too: show it with what still blocks it.
    if plan and plan["id"].startswith("offer-") and graph.get("short"):
        supplier_id = next((sid for sid, name in suppliers.items() if plan["name"].endswith(name)), None)
        open_steps = [s for s in plan.get("steps", [])
                      if s["state"] not in {"confirmed", "superseded"} and not s.get("reminder_of")]
        option = component.get("option") or {}
        conflict = (option.get("deliveryState") or {}).get("conflict")
        departure = option.get("departure") or {}
        if conflict:  # the sent order is disputed: say that, not "waiting for the order"
            meta = "Order sent · supplier disputes dispatch release"
            if departure.get("state") == "passed":
                meta += " · departure deadline passed"
        else:
            meta = (f"Waiting: {', '.join(s['name'].lower() for s in open_steps if s['state'] == 'waiting')}"
                    if any(s["state"] == "waiting" for s in open_steps) else "Not ordered yet")
        node = {"id": plan["id"], "label": f"{plan['name'].split(' from ')[0]}, {plan.get('quantity', 0):,}",
                "kind": "shipment", "status": "at_risk", "meta": meta, "subtitle": "Ready stock",
                "detail": (f"Still open: {', '.join(s['name'].lower() for s in open_steps)}. Each step is shown under Where it stands."
                           if open_steps else None)}
        graph["nodes"].append(node)
        if supplier_id:
            graph["edges"].append({"source": supplier_id, "target": plan["id"], "label": "rush supply"})
    for node in graph["nodes"]:
        if node["kind"] not in {"risk", "shipment"} or node["id"].startswith("later:"):
            continue
        if not graph.get("short"):
            node["doing"] = f"Nothing to do: {rules.cfg('site_name')} already has enough of this part."
        elif node["id"] in by_route:
            node["doing"] = route_doing(by_route[node["id"]])
        else:
            name = suppliers.get(supplier_of.get(node["id"], ""))
            own = [r for r in routes if name and r["name"].endswith(name)]
            active = next((r for r in own if r is plan), None) or next(
                (r for r in own if (r.get("next_step") or {}).get("action_id")), None)
            node["doing"] = route_doing(active) if active else idle
            if active is plan and plan["id"].startswith("offer-") and node.get("meta", "").startswith("Pauses production"):
                node["doing"] = "No effect on the rush order: it ships stock that is already made."
    return graph
