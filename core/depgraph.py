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
        if condition := fact.get("unblock_condition"):
            summaries[f"Release condition — {condition}"] = None
        upstream = SUB_SUPPLIER.search(body)
        export_licence = re.search(r"export.{0,25}licen[cs]|licen[cs].{0,25}export", detail or "", re.I)
        text = " ".join(filter(None, [detail, *summaries])).lower()
        title = ("Export licence" if export_licence else
                 "Release pending" if re.search(r"ppap|written release|qualif", text) else
                 "Downtime" if re.search(r"downtime|maintenance|przest", text) else
                 "Failed inspection" if re.search(r"inspection|aql|reject", text) else RISK_TYPES[kind])
        detail = " ".join(filter(None, [detail, *summaries]))
        yield supplier, path, title, upstream[1].strip() if upstream else title, detail or None, scoped_parts, (
            "blocked" if kind == "supplier_hold" else "at_risk"
        )


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

        for product in sorted(using_products(component_id)):
            picture = evaluation["picture"].get(product, {})
            short = picture.get("slack_copies", 0) < 0 or any(
                row["need"].get(product, 0) > row["alloc"].get(product, 0) and not row["suspect_master_data"]
                for row in coverage.values()
            )
            add_node(product, re.sub(r"\s+\([^)]*\)$", "", products[product] or "") or "Finished product",
                     "product", "at_risk" if short else "ok", code=product)
            assemblies = [parent for parent in upstream if product in using_products(parent)]
            for part in assemblies or [component_id]:
                add_edge(product, part, quantity_label(bom_parents[part].get(product)))

        supplied_parts = defaultdict(set)
        fitted_parents = [parent for parent in upstream if children[parent][component_id]]
        materials = [component_id, *subparts, *fitted_parents]
        for part in materials:
            for supplier, name, status, share, primary in sources[part]:
                supplied_parts[supplier].add(part)
                status = (status or "").casefold().strip()
                health = "blocked" if status in {"blocked", "suspended", "on hold", "inactive"} else (
                    "ok" if status == "active" else "at_risk"
                )
                add_node(supplier, name or "Supplier", "supplier", health, status.capitalize() or "Status unconfirmed.", code=supplier)
                primary = str(primary).lower() in {"y", "yes", "true", "1"}
                detail = f"{share:.0%} of supply" if share is not None else ""
                if primary:
                    detail = f"Primary supplier. {detail}" if detail else "Primary supplier"
                add_edge(part, supplier, "made by", detail=detail or None)

        for supplier, path, title, name, detail, scoped_parts, status in supplier_risks(conn, supplied_parts):
            risk = f"risk:{supplier}:{path}"
            generic = (name or "").strip().lower() in {"", "supply risk", "risk"}
            add_node(risk, (title if generic and title else name) or "Supply risk", "risk", status, detail,
                     subtitle=None if generic else title, evidence=path)
            add_edge(supplier, risk, "supply risk")
            for part in supplied_parts[supplier] & scoped_parts:
                nodes[part]["status"] = "at_risk"

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
            uncertain = (status in {"booked", "delayed"} or shipment["flags"]
                         or (shipment["planned_for_launch"] and not shipment["makes_cutoff"]))
            shipment_id = shipment["shipment_id"]
            mode = mode_names.get(shipment["mode"], "Shipment")
            if stuck:
                label = f"{mode}, at the port" if status == "arrived_port" else f"{mode}, held"
            else:
                label = f"{mode}, {arrival.strftime('%a %H:%M') if arrival - rules.now() < __import__('datetime').timedelta(days=3) else arrival.strftime('%d %b').lstrip('0')}"
            detail = [status.replace("_", " ").capitalize(), carrier_names.get(shipment["carrier_id"]), *shipment["flags"]]
            if shipment["planned_for_launch"] and shipment["makes_cutoff"] is False:
                detail.append("Misses the receiving cutoff")
            add_node(shipment_id, label, "shipment", "blocked" if held or stuck else "at_risk" if uncertain else "ok",
                     ". ".join(filter(None, detail)), code=shipment_id, reference=shipment["reference"],
                     subtitle="Arrives before launch" if not stuck and shipment["makes_cutoff"] else "Inbound shipment")
            add_edge(parent, shipment_id, "held" if held else "stuck" if stuck else "on its way")
        for parent, rows in later.items():
            first, last = rows[0][0], rows[-1][0]
            span = first.strftime("%d %b").lstrip("0") + ("" if len(rows) == 1 else f" to {last.strftime('%d %b').lstrip('0')}")
            nid = f"later:{parent}"
            add_node(nid, f"{len(rows)} later {'delivery' if len(rows) == 1 else 'deliveries'}, {span}", "shipment", "ok",
                     "Arrive after the trucks leave, so they do not help this launch: " + ", ".join(r[1].get("reference") or r[1]["shipment_id"] for r in rows),
                     subtitle="After launch")
            add_edge(parent, nid, "after launch")

    return {"nodes": list(nodes.values()), "edges": edges}
