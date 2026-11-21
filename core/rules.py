"""Deterministic detectors over the master DB.

Output: a launch picture (per-component coverage, CE copies at risk in EUR) and a list of findings,
each with evidence (source files), an EUR impact and a suggested action with the approver it needs.
The LLM agent sits on top of this: it explains, drafts messages and asks for approval. The numbers come from here.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "soony.db"
CONFIG = json.loads((ROOT / "config" / "business.json").read_text())


def cfg(key):
    return CONFIG[key]["value"]


TZ = dt.timezone(dt.timedelta(hours=1))


def now() -> dt.datetime:
    """Wall clock, or SOONY_NOW for replays / demo rehearsal."""
    v = os.environ.get("SOONY_NOW")
    return dt.datetime.fromisoformat(v) if v else dt.datetime.now(TZ)


def ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    d = dt.datetime.fromisoformat(s) if "T" in s else dt.datetime.fromisoformat(s + "T12:00:00")
    return d if d.tzinfo else d.replace(tzinfo=TZ)


@dataclass
class Finding:
    id: str
    kind: str
    severity: str  # critical | high | medium | info
    title: str
    detail: str
    entity: str | None = None
    eur_at_risk: float = 0
    ce_copies: int = 0
    evidence: list = field(default_factory=list)
    action: dict | None = None


def plain(desc: str | None, fallback: str = "") -> str:
    """'Art Book Grand Heist 6 (96 p, hardcover)' -> 'Art book'. Users never see part codes in titles."""
    if not desc:
        return fallback
    d = re.sub(r"\s*\(.*?\)", "", desc)
    d = re.sub(r"\s*[–-]?\s*Grand Heist \d+", "", d).strip(" –-")
    d = re.sub(r'\s*".*?"', "", d).strip()
    return d[:1].upper() + d[1:] if d else fallback


def approver_for(eur: float, category: str | None = None) -> dict:
    people = cfg("people")
    for tier in cfg("approval_ladder"):
        if tier["max_eur"] is None or eur < tier["max_eur"]:
            role = tier["role"]
            name = people.get(f"{role}:{category}") if category else None
            if role in ("Buyer", "Category Lead") and category and name is None:
                # vacant seat (e.g. Collector's Category Lead this weekend) -> escalate one tier
                if role == "Category Lead":
                    nxt = cfg("approval_ladder")[2]["role"]
                    return {"role": nxt, "name": people.get(nxt), "note": f"Category Lead {category} vacant -> escalated"}
            return {"role": role, "name": name or people.get(role), "limit_eur": tier["max_eur"]}
    return {"role": "unknown"}


def shift_hours_between(a: dt.datetime, b: dt.datetime) -> float:
    h = 0.0
    for s in cfg("shifts"):
        st, en = ts(s["start"]), ts(s["end"])
        lo, hi = max(a, st), min(b, en)
        if hi > lo:
            h += (hi - lo).total_seconds() / 3600
    return h


def facts(con, ftype: str) -> list[dict]:
    return [dict(json.loads(p), _path=path) for path, p in con.execute("SELECT path, payload FROM doc_facts WHERE fact_type=?", (ftype,))]


# ---------------------------------------------------------------- launch picture
def launch_picture(con) -> dict:
    t_now = now()
    deadline = ts(cfg("deadline"))
    latest = {}
    for pid, as_of, demand, packed, todo, rate, src in con.execute("SELECT * FROM launch_status ORDER BY as_of"):
        latest[pid] = dict(product_id=pid, as_of=as_of, demand=demand, packed=packed, still_to_pack=todo, rate=rate, source=src)
    snap = {m: q for m, q in con.execute("SELECT material_id, quantity FROM stock WHERE material_id LIKE 'FG-%'")}
    for pid, p in latest.items():
        start = max(t_now, ts(p["as_of"]))
        # packing continues between as_of and now at the stated rate
        since = shift_hours_between(ts(p["as_of"]), start)
        p["packed_est_now"] = min(p["demand"], p["packed"] + round(since * p["rate"]))
        p["hours_left"] = round(shift_hours_between(start, deadline), 1)
        p["capacity_left"] = round(p["hours_left"] * p["rate"])
        p["todo_now"] = p["demand"] - p["packed_est_now"]
        p["slack_copies"] = p["capacity_left"] - p["todo_now"]
        p["packed_at_stock_snapshot"] = snap.get(pid)
    return latest


def component_coverage(con, picture: dict) -> dict:
    """Usable stock vs. remaining need per component, CE first."""
    prio = cfg("priority_product")
    bom = {}
    for parent, child, qty, rev, use_up, eff, chg in con.execute("SELECT * FROM bom WHERE parent_id IN (%s)" % ",".join("?" * len(picture)), list(picture)):
        bom.setdefault(child, {})[parent] = dict(qty=qty, rev=rev, use_up=use_up, eff=eff, change=chg)
    gr = {r[0]: dict(zip(("gr_number", "gr_date", "po_number", "po_line", "supplier_id", "material_id", "qty_ordered", "qty_received", "delivery_note", "inspector", "remark"), r))
          for r in con.execute("SELECT * FROM goods_receipts")}
    rev_facts = {(f.get("po_number"), f.get("material_id")): f for f in facts(con, "artwork_revision")}
    rev_by_supplier = {}
    for f in rev_facts.values():
        rev_by_supplier.setdefault((f.get("supplier_id"), f.get("material_id")), set()).add(f.get("revision"))
    inspections = {f.get("gr_number"): f for f in facts(con, "inspection")}
    corrections = {f.get("delivery_note"): f for f in facts(con, "receipt_correction")}
    po_hist = {m for (m,) in con.execute("SELECT DISTINCT material_id FROM purchase_orders")}

    out = {}
    for comp, uses in bom.items():
        desc = con.execute("SELECT description FROM components WHERE component_id=?", (comp,)).fetchone()
        need = {p: int(picture[p]["todo_now"] * u["qty"]) for p, u in uses.items()}
        consumed_since = int(sum((picture[p]["packed_est_now"] - (picture[p]["packed_at_stock_snapshot"] or picture[p]["packed_est_now"])) * u["qty"] for p, u in uses.items()))
        batches, adjustments = [], []
        for (batch, qty) in con.execute("SELECT batch, quantity FROM stock WHERE material_id=?", (comp,)):
            usable, reasons = qty, []
            g = gr.get(batch)
            if g:
                if g["qty_ordered"] and g["qty_received"] and g["qty_received"] >= 1.5 * g["qty_ordered"]:
                    c = corrections.get(g["delivery_note"])
                    corrected = (c or {}).get("qty_corrected") or g["qty_ordered"]
                    cut = min(usable, g["qty_received"] - corrected)
                    usable -= cut
                    reasons.append(dict(kind="over_receipt", cut=cut, detail=f"GR {batch}: received {g['qty_received']:,} vs ordered {g['qty_ordered']:,}"
                                        + (f"; delivery note {g['delivery_note']} hand-corrected to {corrected:,}" if c else "; planning with ordered qty"),
                                        evidence=["soony/erp/goods_receipts.csv"] + ([c["_path"]] if c else [])))
                insp = inspections.get(batch)
                if insp and str(insp.get("result", "")).lower().startswith("reject"):
                    reasons.append(dict(kind="quality_reject", cut=usable, detail=f"Lot {batch} failed incoming inspection: {insp.get('summary', '')}".strip(),
                                        evidence=[insp["_path"]], condition=insp.get("release_condition")))
                    usable = 0
                for parent, u in uses.items():
                    if u["use_up"] == "N" and u["rev"]:
                        f = rev_facts.get((g["po_number"], comp))
                        revs = {f["revision"]} if f else rev_by_supplier.get((g["supplier_id"], comp), set())
                        if revs and u["rev"] not in revs:
                            reasons.append(dict(kind="old_revision", cut=usable, detail=f"Batch {batch} from {g['supplier_id']} printed to rev {'/'.join(sorted(revs))}; {u['change']} requires rev {u['rev']}, no use-up",
                                                evidence=[f["_path"]] if f else ["doc_facts (supplier)"]))
                            usable = 0
                        elif not revs:
                            reasons.append(dict(kind="revision_unverified", cut=0, detail=f"Batch {batch}: revision not confirmed by any document ({u['change']} requires rev {u['rev']})"))
            batches.append(dict(batch=batch, qty=qty, usable=usable, supplier=(g or {}).get("supplier_id")))
            adjustments += reasons
        erp_stock = sum(b["qty"] for b in batches)
        usable_now = int(sum(b["usable"] for b in batches) - consumed_since)
        suspect_master = erp_stock == 0 and comp not in po_hist and any((picture[p]["packed_at_stock_snapshot"] or 0) > 0 for p in uses)
        # CE first: priority product draws on usable stock before anyone else.
        alloc, left = {}, usable_now
        for p in sorted(uses, key=lambda x: x != prio):
            alloc[p] = max(0, min(left, need[p]))
            left -= alloc[p]
        out[comp] = dict(component=comp, description=desc[0] if desc else comp, need=need, need_total=sum(need.values()),
                         erp_stock=erp_stock, consumed_since_snapshot=consumed_since, usable_now=usable_now, gap=usable_now - sum(need.values()),
                         alloc=alloc, adjustments=adjustments, batches=batches, suspect_master_data=suspect_master)
    return out


# ---------------------------------------------------------------- inbound
def inbound(con, coverage: dict) -> list[dict]:
    t_now, deadline = now(), ts(cfg("deadline"))
    cutoff = deadline - dt.timedelta(hours=cfg("receiving_buffer_hours"))
    routes = {r[0]: dict(zip(("route_id", "origin", "destination", "mode", "carrier_id", "transit_days", "via"), r)) for r in con.execute("SELECT * FROM routes")}
    ports = dict(con.execute("SELECT locode, mode FROM ports").fetchall())
    advice = {f.get("reference"): f for f in facts(con, "shipping_advice")}
    holds = facts(con, "supplier_hold")
    cols = [d[0] for d in con.execute("SELECT * FROM shipments LIMIT 0").description]
    out = []
    for row in con.execute("SELECT * FROM shipments"):
        s = dict(zip(cols, row))
        flags, ev = [], ["soony/erp/open_shipments.csv"]
        r = routes.get(s["route_id"], {})
        etd, eta = ts(s["etd"]), ts(s["eta"])
        upd = ts(s["updated_at"])
        # 1) ETA plausibility vs. route standard
        if r.get("transit_days") and eta and etd and (eta - etd).days < r["transit_days"]:
            flags.append(f"ETA assumes {(eta - etd).days} d, route standard is {r['transit_days']:g} d")
            eta = etd + dt.timedelta(days=r["transit_days"])
        # 2) status vs. clock
        if s["status"] == "booked" and etd and etd.date() < t_now.date():
            slip = t_now.date() - etd.date()
            flags.append(f"ETD {s['etd']} passed but still 'booked' (no departure confirmed)")
            eta = eta + slip if eta else eta
        stale_h = (t_now - upd).total_seconds() / 3600 if upd else None
        if stale_h and stale_h > cfg("staleness_hours").get(s["mode"], 24) and s["status"] not in ("booked",):
            flags.append(f"no carrier update for {stale_h:.0f} h")
        # 3) last mile: port/airport -> DC
        last_leg = next((x for x in routes.values() if x["origin"] == s["destination"] and x["destination"] == s["dest_site"]), None)
        at_dc = eta
        if s["destination"] != s["dest_site"] and last_leg:
            at_dc = (eta or t_now) + dt.timedelta(days=last_leg["transit_days"])
        if s["status"] == "arrived_port":
            at_dc = None  # nothing moves until someone books the truck
            flags.append(f"at {s['destination']} since {s['eta']}: waiting for pick-up, no truck booked")
        # 4) documents override the ERP
        qty = s["quantity"]
        a = advice.get(s["reference"])
        if a:
            ev.append(a["_path"])
            if a.get("qty_shipped") is not None and qty and a["qty_shipped"] < qty:
                flags.append(f"short shipment: {int(a['qty_shipped']):,} of {int(qty):,} shipped; balance {a.get('balance', qty - a['qty_shipped']):,} ETA {a.get('balance_eta', '?')}")
                qty = a["qty_shipped"]
        for h in holds:
            if h.get("po_number") and h["po_number"] in (s["po_numbers"] or ""):
                flags.append(f"supplier hold: {h.get('reason')} -> {h.get('unblock_condition')}")
                ev.append(h["_path"])
                at_dc = None
        # 5) Sunday truck ban on the final road leg (port-road combined transport exempt)
        ban = cfg("truck_sunday_ban")
        road_from_port = last_leg and ports.get(s["destination"]) == "sea"
        if at_dc and at_dc.weekday() == ban["weekday"] and at_dc.hour < 22 and (s["mode"] in ("air", "road")) and not road_from_port:
            flags.append(f"arrives Sunday {at_dc:%H:%M}: DE Sunday truck ban (>{ban['min_weight_t']} t) applies to the road leg -> van <=7.5 t, permit, or arrive Saturday")
        mats = (s["material_ids"] or "").split("|")
        critical_for = [m for m in mats if m in coverage and coverage[m]["gap"] < 0]
        erp_eta = ts(s["eta"])
        planned_at_dc = erp_eta + dt.timedelta(days=last_leg["transit_days"]) if (erp_eta and last_leg and s["destination"] != s["dest_site"]) else erp_eta
        planned_for_launch = bool(planned_at_dc and planned_at_dc <= cutoff)
        out.append(dict(shipment_id=s["shipment_id"], mode=s["mode"], status=s["status"], reference=s["reference"], supplier_id=s["supplier_id"],
                        carrier_id=s["carrier_id"], materials=mats, quantity=qty, po_numbers=s["po_numbers"], origin=s["origin"], destination=s["destination"],
                        etd=s["etd"], eta_erp=s["eta"], arrival_at_dc=at_dc.isoformat() if at_dc else None,
                        makes_cutoff=(at_dc <= cutoff) if at_dc else None, cutoff=cutoff.isoformat(), flags=flags, evidence=ev,
                        fills_gap_of=critical_for, planned_for_launch=planned_for_launch, last_lat=s["last_lat"], last_lon=s["last_lon"], updated_at=s["updated_at"]))
    return out


# ---------------------------------------------------------------- findings
def ce_copies_short(coverage: dict, picture: dict) -> tuple[int, str | None]:
    prio = cfg("priority_product")
    worst, comp = 0, None
    for c in coverage.values():
        if prio not in c["need"] or c["suspect_master_data"]:
            continue
        short = c["need"][prio] - c["alloc"][prio]
        if short > worst:
            worst, comp = short, c["component"]
    cap_short = max(0, -picture[prio]["slack_copies"])
    if cap_short > worst:
        return cap_short, "line capacity"
    return int(worst), comp


def evaluate(db_path: Path = DB_PATH) -> dict:
    con = sqlite3.connect(db_path)
    pic = launch_picture(con)
    cov = component_coverage(con, pic)
    inb = inbound(con, cov)
    prio = cfg("priority_product")
    price = cfg("missed_copy_cost_eur")[prio]
    findings: list[Finding] = []
    names = dict(con.execute("SELECT supplier_id, name FROM suppliers").fetchall())
    sup_name = lambda sid: names.get(sid, sid)

    short, binding = ce_copies_short(cov, pic)
    findings.append(Finding("ce-at-risk", "launch", "critical" if short else "info",
                            f"{short:,} Collector's copies at risk (EUR {short * price:,.0f} refunds)" if short else "Collector's Edition fully covered",
                            f"Binding constraint: {plain(cov[binding]['description']) if binding in cov else binding}. CE slack on the cell: {pic[prio]['slack_copies']:,} copies "
                            f"({pic[prio]['hours_left']} pack hours left).", prio, short * price, short))

    for c in cov.values():
        ce_short = c["need"].get(prio, 0) - c["alloc"].get(prio, 0)
        if c["suspect_master_data"]:
            notes = con.execute("SELECT s.folder, sc.note FROM supplier_components sc JOIN suppliers s USING(supplier_id) WHERE component_id=? AND note<>''",
                                (c["component"],)).fetchall()
            explain = " ".join(f"Supplier says: \"{n}\" ({f}/components.csv)." for f, n in notes)
            findings.append(Finding(f"master-{c['component']}", "data", "medium" if notes else "high",
                                    f"{plain(c['description'])}: listed as a separate part but never ordered or stocked",
                                    (explain + " " if explain else "") + "BOM line treats it as a separate part; excluded from the risk maths. Fix the BOM after launch.",
                                    c["component"], evidence=["soony/master/bom.csv", "soony/erp/stock_snapshot.csv"] + [f"suppliers/{f}/components.csv" for f, _ in notes],
                                    action=dict(type="ask", to=cfg("people")["Head of Procurement"], question=f"Is {c['component']} ({c['description']}) really a separate part of {prio}?")))
            continue
        for adj in c["adjustments"]:
            if adj["kind"] == "revision_unverified":
                continue
            findings.append(Finding(f"{adj['kind']}-{c['component']}-{len(findings)}", "stock", "high" if adj["cut"] else "medium",
                                    f"{plain(c['description'])}: {int(adj['cut']):,} in stock can't be used ({adj['kind'].replace('_', ' ')})", adj["detail"], c["component"],
                                    evidence=adj.get("evidence", [])))
        if c["gap"] < 0:
            findings.append(Finding(f"gap-{c['component']}", "gap", "critical" if ce_short > 0 else "high",
                                    f"{plain(c['description'])}: {-c['gap']:,} short" + (f", stops {ce_short:,} Collector's copies" if ce_short > 0 else " for Standard"),
                                    f"Need {c['need_total']:,}, usable {c['usable_now']:,} (ERP {c['erp_stock']:,}, consumed since snapshot {c['consumed_since_snapshot']:,}).",
                                    c["component"], ce_short * price if ce_short > 0 else 0, max(ce_short, 0)))

    for s in inb:
        if not (s["fills_gap_of"] or s["status"] == "arrived_port"):
            continue
        if not s["planned_for_launch"] and s["status"] != "arrived_port":
            continue  # was never going to make Sunday; not news
        sev = "critical" if s["makes_cutoff"] is not True and s["fills_gap_of"] else "high"
        verdict = "makes the cut-off" if s["makes_cutoff"] else (("waiting at the port, no truck booked" if s["status"] == "arrived_port" else "held at the supplier") if s["arrival_at_dc"] is None else "misses the cut-off")
        findings.append(Finding(f"ship-{s['shipment_id']}", "shipment", sev, f"{', '.join(plain(cov[m]['description']) for m in s['materials'] if m in cov) or s['mode']} by {s['mode']} ({s['reference']}): {verdict}",
                                "; ".join(s["flags"]) or "on plan", s["shipment_id"], evidence=s["evidence"]))

    for o in facts(con, "offer"):
        mat = o.get("material_id")
        if mat in cov and cov[mat]["gap"] < 0:
            ce_short = cov[mat]["need"].get(prio, 0) - cov[mat]["alloc"].get(prio, 0)
            closes = min(o.get("qty") or 0, -cov[mat]["gap"])
            cost = (o.get("unit_price_eur") or 0) * (o.get("qty") or 0)
            findings.append(Finding(f"offer-{o.get('supplier_id')}-{mat}", "opportunity", "critical",
                                    f"{sup_name(o.get('supplier_id'))} offers {o.get('qty'):,} {plain(cov[mat]['description']).lower()}s, closes {closes:,} of the gap",
                                    f"{o.get('lead_time_note', '')} Valid until {o.get('valid_until', '?')}. Cost ≈ EUR {cost:,.0f} vs EUR {min(closes, ce_short) * price:,.0f} refunds avoided.",
                                    mat, min(closes, ce_short) * price, min(closes, ce_short), evidence=[o["_path"]],
                                    action=dict(type="order_and_truck", cost_eur=cost, approver=approver_for(cost, "Collector's Items"))))

    # Supplier health: status changes and expired qualifications, weighted by the CE parts the supplier delivers.
    t_now = now()
    for sid, name, status, qual in con.execute("SELECT supplier_id, name, status, qualification_status FROM suppliers"):
        obs = {f: (v, src) for f, v, src in con.execute("SELECT field, value, source FROM observations WHERE entity='supplier' AND entity_id=? ORDER BY as_of", (sid,))}
        problems, ev = [], []
        if status and status.lower() not in ("active",):
            problems.append(f"status '{status}'")
            ev.append(obs.get("status", (None, None))[1])
        vu = obs.get("qualification.validUntil")
        if vu and vu[0] and ts(vu[0]) and ts(vu[0]) < t_now and not (obs.get("qualification.extendedBy") or (None,))[0]:
            problems.append(f"{qual or ''} qualification expired {vu[0]}, not extended")
            ev.append(vu[1])
        if not problems:
            continue
        parts = [c for (c,) in con.execute("SELECT component_id FROM supplier_components WHERE supplier_id=?", (sid,))]
        ce_parts = [c for c in parts if c in cov and prio in cov[c]["need"]]
        note = (obs.get("notes") or (None,))[0]
        findings.append(Finding(f"supplier-{sid}", "supplier", "high" if ce_parts else "medium", f"{name} ({sid}): " + "; ".join(problems),
                                (f"Vendor master note: {note} " if note else "") + (f"Delivers Collector's parts: {', '.join(ce_parts)}." if ce_parts else ""),
                                sid, evidence=[e for e in ev if e]))

    order = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    findings.sort(key=lambda f: (order[f.severity], -f.eur_at_risk))
    con.close()
    return dict(now=now().isoformat(), picture=pic, coverage=cov, inbound=inb, findings=[asdict(f) for f in findings])


if __name__ == "__main__":
    r = evaluate()
    p = r["picture"]
    for pid, x in p.items():
        print(f"{pid}: packed≈{x['packed_est_now']:,}/{x['demand']:,}  todo {x['todo_now']:,}  hours left {x['hours_left']}  slack {x['slack_copies']:,}")
    print()
    for c in r["coverage"].values():
        print(f"{c['component']:12} need {c['need_total']:>8,}  usable {c['usable_now']:>8,}  gap {c['gap']:>9,}  {'SUSPECT' if c['suspect_master_data'] else ''}")
    print()
    for s in r["inbound"]:
        if s["fills_gap_of"] or s["flags"]:
            print(f"{s['shipment_id']} {s['mode']:4} {s['status']:12} at_dc={s['arrival_at_dc']} ok={s['makes_cutoff']} gaps={s['fills_gap_of']}")
            for fl in s["flags"]:
                print("     -", fl)
    print()
    for f in r["findings"]:
        print(f"[{f['severity']:8}] {f['title']}  (EUR {f['eur_at_risk']:,.0f})")
