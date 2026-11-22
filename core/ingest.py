"""Build the master database (SQLite) from the raw portal files.

Principles
- Deterministic rebuild: delete + rebuild from data/current on every change. A new drop never needs code.
- Nothing is overwritten silently: every value that more than one source talks about lands in
  `observations` (entity, id, field, value, source, as_of). Disagreements show up in the `conflicts` view.
- Every CSV / XLSX sheet is also loaded verbatim into a raw_* table, so a file we never saw before is
  still queryable by the agent.
- Parse problems go to `ingest_issues` instead of crashing.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import customer, rules

DATA = ROOT / "data"
CURRENT = DATA / "current"
DB_PATH = rules.DB_PATH

SCHEMA = """
CREATE TABLE sources(path TEXT PRIMARY KEY, kind TEXT, sha256 TEXT, last_changed TEXT);
CREATE TABLE ingest_issues(path TEXT, issue TEXT);
CREATE TABLE observations(entity TEXT, entity_id TEXT, field TEXT, value TEXT, source TEXT, as_of TEXT, note TEXT);
CREATE TABLE products(product_id TEXT PRIMARY KEY, name TEXT, lifecycle TEXT, launch_date TEXT, day_one_qty INTEGER, std_cost_eur REAL);
CREATE TABLE components(component_id TEXT PRIMARY KEY, description TEXT, category TEXT, critical TEXT, drawing_rev TEXT);
CREATE TABLE bom(parent_id TEXT, child_id TEXT, qty_per REAL, rev TEXT, use_up_old_rev TEXT, rev_effective_from TEXT, change_id TEXT);
CREATE TABLE suppliers(supplier_id TEXT PRIMARY KEY, name TEXT, country TEXT, city TEXT, incoterm TEXT, status TEXT,
  qualification_status TEXT, contact_name TEXT, contact_email TEXT, phone TEXT, language TEXT, folder TEXT);
CREATE TABLE supplier_components(supplier_id TEXT, component_id TEXT, description TEXT, share REAL, unit_price REAL, currency TEXT,
  lead_time_days REAL, moq REAL, is_primary TEXT, note TEXT);
CREATE TABLE carriers(carrier_id TEXT PRIMARY KEY, name TEXT, city TEXT, country TEXT, modes TEXT);
CREATE TABLE ports(locode TEXT PRIMARY KEY, name TEXT, country TEXT, lat REAL, lon REAL, mode TEXT);
CREATE TABLE sites(site_id TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL);
CREATE TABLE routes(route_id TEXT PRIMARY KEY, origin TEXT, destination TEXT, mode TEXT, carrier_id TEXT, transit_days REAL, via TEXT);
CREATE TABLE purchase_orders(po_number TEXT, po_line INTEGER, supplier_id TEXT, material_id TEXT, plant TEXT, quantity REAL,
  unit_price REAL, currency TEXT, order_date TEXT, requested_date TEXT, promised_date TEXT, actual_delivery_date TEXT,
  status TEXT, buyer TEXT, incoterm TEXT, note TEXT, source TEXT);
CREATE TABLE goods_receipts(gr_number TEXT PRIMARY KEY, gr_date TEXT, po_number TEXT, po_line INTEGER, supplier_id TEXT,
  material_id TEXT, qty_ordered REAL, qty_received REAL, delivery_note TEXT, inspector TEXT, remark TEXT);
CREATE TABLE stock(snapshot_date TEXT, material_id TEXT, batch TEXT, quantity REAL, last_count TEXT);
CREATE TABLE shipments(shipment_id TEXT PRIMARY KEY, carrier_id TEXT, mode TEXT, reference TEXT, supplier_id TEXT,
  po_numbers TEXT, material_ids TEXT, route_id TEXT, origin TEXT, destination TEXT, via TEXT, dest_site TEXT,
  etd TEXT, eta TEXT, status TEXT, last_lat REAL, last_lon REAL, updated_at TEXT, note TEXT, quantity REAL);
CREATE TABLE launch_status(product_id TEXT, as_of TEXT, demand INTEGER, packed INTEGER, still_to_pack INTEGER, rate_per_hour REAL, source TEXT);
CREATE TABLE documents(path TEXT PRIMARY KEY, supplier_id TEXT, doc_type TEXT, po_number TEXT, text TEXT);
CREATE TABLE doc_facts(path TEXT, fact_type TEXT, payload TEXT, extracted_by TEXT);
CREATE VIEW conflicts AS
  SELECT entity, entity_id, field, COUNT(DISTINCT lower(value)) AS n_values,
         json_group_array(json_object('value', value, 'source', source, 'as_of', as_of)) AS claims
  FROM observations WHERE value IS NOT NULL AND value <> ''
  GROUP BY entity, entity_id, field HAVING COUNT(DISTINCT lower(value)) > 1;
"""

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ---------- parsing helpers ----------
def num(v, decimal_comma: bool | None = None):
    """Parse numbers like 640.000 / 5,600 / 5 600 / 1,5200 / 34.5. decimal_comma forces interpretation."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    if decimal_comma is True:
        s = s.replace(".", "").replace(",", ".")
    elif decimal_comma is False:
        s = s.replace(",", "")
    elif "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif re.fullmatch(r"-?\d{1,3}([.,]\d{3})+", s):  # 640.000 or 5,600 -> thousands separators
        s = s.replace(".", "").replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def date_iso(v):
    if v in (None, ""):
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.date().isoformat() if isinstance(v, dt.datetime) else v.isoformat()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return s


def read_csv(path: Path) -> list[dict]:
    raw = path.read_bytes().decode("utf-8-sig", errors="replace")
    first = raw.splitlines()[0] if raw else ""
    delim = ";" if first.count(";") > first.count(",") else ","
    return list(csv.DictReader(io.StringIO(raw), delimiter=delim))


def table_name(rel: str) -> str:
    return "raw_" + re.sub(r"[^a-z0-9]+", "_", rel.lower()).strip("_")[:120]


def load_raw(con, name: str, rows: list[dict], src: str):
    if not rows:
        return
    cols = list(dict.fromkeys(k for r in rows for k in r.keys() if k))
    con.execute(f'DROP TABLE IF EXISTS "{name}"')
    con.execute(f'CREATE TABLE "{name}" ({", ".join(f"""\"{c}\" TEXT""" for c in cols)}, _source TEXT)')
    con.executemany(f'INSERT INTO "{name}" VALUES ({",".join("?" * (len(cols) + 1))})',
                    [[None if r.get(c) is None else str(r.get(c)) for c in cols] + [src] for r in rows])


def xlsx_sheets(path: Path) -> dict[str, list[tuple]]:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    return {ws.title: [r for r in ws.iter_rows(values_only=True) if any(c is not None for c in r)] for ws in wb.worksheets}


def header_rows(rows: list[tuple], must_contain: str) -> list[dict]:
    """Find the header row containing `must_contain` and return the rows below as dicts."""
    for i, r in enumerate(rows):
        if any(isinstance(c, str) and c.strip().lower() == must_contain.lower() for c in r):
            hdr = [str(c).strip() if c is not None else f"col{j}" for j, c in enumerate(r)]
            return [dict(zip(hdr, x)) for x in rows[i + 1:]]
    return []


def flatten(d, prefix=""):
    out = {}
    for k, v in (d.items() if isinstance(d, dict) else []):
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
    return out


def json_rows(d) -> list[dict]:
    """A JSON file as table rows: the first list of objects found becomes rows, otherwise one flattened row."""
    if isinstance(d, list):
        return [flatten(x) for x in d if isinstance(x, dict)]
    for v in d.values():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return [flatten(x) for x in v]
    return [flatten(d)]


def pdf_text(path: Path) -> str:
    import subprocess
    try:
        return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""


# ---------- loaders ----------
def build(db_path: Path = DB_PATH, current: Path = CURRENT, *, facts_dir: Path | None = None) -> dict:
    sources = rules.cfg("sources")
    paths, columns = sources["paths"], sources.get("columns", {})
    facts_dir = facts_dir if facts_dir is not None else current.parent / "doc_facts"

    def mapped(key, rows):
        aliases = columns.get(key, {})
        for row in rows:
            yield {**row, **{field: row[header] for field, header in aliases.items() if header in row}}

    def read(key, path=None):
        return mapped(key, read_csv(path if path is not None else current / paths[key]))

    def matches(key, root=current):
        return sorted(root.glob(paths[key])) if paths.get(key) else []

    def workbook_rows(key, rows, field):
        return mapped(key, header_rows(rows, columns.get(key, {}).get(field, field)))

    tmp = db_path.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    issues = []

    def issue(path, msg):
        issues.append((path, msg))

    def obs(entity, eid, field, value, source, as_of=None, note=None):
        con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?)",
                    (entity, eid, field, None if value is None else str(value), source, as_of, note))

    manifest_path = current.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    files = sorted(p for p in current.rglob("*") if p.is_file())
    for p in files:
        rel = str(p.relative_to(current))
        m = manifest.get(rel, {})
        con.execute("INSERT INTO sources VALUES (?,?,?,?)", (rel, p.suffix.lower().lstrip("."), m.get("sha256"), m.get("last_changed")))
        try:
            if p.suffix.lower() == ".csv":
                load_raw(con, table_name(rel), read_csv(p), rel)
            elif p.suffix.lower() == ".json":
                load_raw(con, table_name(rel), json_rows(json.loads(p.read_text())), rel)
            elif p.suffix.lower() == ".xlsx":
                for sheet, rows in xlsx_sheets(p).items():
                    hdr_idx = next((i for i, r in enumerate(rows) if sum(c is not None for c in r) >= 3), 0)
                    hdr = [str(c) if c is not None else f"col{j}" for j, c in enumerate(rows[hdr_idx])] if rows else []
                    load_raw(con, table_name(f"{rel}_{sheet}"), [dict(zip(hdr, r)) for r in rows[hdr_idx + 1:]], f"{rel}#{sheet}")
        except Exception as e:
            issue(rel, f"raw load failed: {e}")

    def safe(fn):
        try:
            fn()
        except FileNotFoundError as e:
            issue(fn.__name__, f"missing file: {e.filename}")
        except Exception as e:
            issue(fn.__name__, f"{type(e).__name__}: {e}")

    def master():
        for r in read("products"):
            con.execute("INSERT INTO products VALUES (?,?,?,?,?,?)", (r["product_id"], r["name"], r["lifecycle"], r["launch_date"],
                        num(r.get("day_one_qty")), num(r.get("std_cost_eur"), False)))
        comps = {r["component_id"]: r for r in read("components")}
        for f in matches("component_updates"):
            for u in read("component_updates", f):
                c = comps.get(u["component_id"])
                if not c:
                    issue(str(f), f"update for unknown component {u['component_id']}")
                    continue
                obs("component", u["component_id"], u["field"], c.get(u["field"]), paths["components"])
                obs("component", u["component_id"], u["field"], u["new"], str(f.relative_to(current)), note=f"changed_by {u.get('changed_by')}")
                c[u["field"]] = u["new"]
        for c in comps.values():
            con.execute("INSERT INTO components VALUES (?,?,?,?,?)", (c["component_id"], c["description"], c["category"], c.get("critical"), c.get("drawing_rev")))
        bom = {(r["parent_id"], r["child_id"]): dict(r) for r in read("bom")}
        for f in matches("bom_deltas"):
            for d in read("bom_deltas", f):
                k = (d["parent_id"], d["child_id"])
                b = bom.setdefault(k, {"parent_id": k[0], "child_id": k[1], "qty_per": d.get("qty_per_new")})
                if d.get("action") == "remove":
                    bom.pop(k, None)
                    continue
                b.update(qty_per=d.get("qty_per_new") or b.get("qty_per"), rev=d.get("rev_new"), use_up_old_rev=d.get("use_up_old_rev"),
                         rev_effective_from=d.get("effective_from"), change_id=d.get("change_id"), rev_old=d.get("rev_old"))
        for b in bom.values():
            con.execute("INSERT INTO bom VALUES (?,?,?,?,?,?,?)", (b["parent_id"], b["child_id"], num(b["qty_per"]), b.get("rev"),
                        b.get("use_up_old_rev"), b.get("rev_effective_from"), b.get("change_id")))
        for r in read("carriers"):
            con.execute("INSERT INTO carriers VALUES (?,?,?,?,?)", (r["carrier_id"], r["name"], r["city"], r["country"], r["modes"]))
        for r in read("ports"):
            con.execute("INSERT INTO ports VALUES (?,?,?,?,?,?)", (r["locode"], r["name"], r["country"], num(r["lat"], False), num(r["lon"], False), r["mode"]))
        for r in read("sites"):
            con.execute("INSERT INTO sites VALUES (?,?,?,?)", (r["site_id"], r["name"], num(r["lat"], False), num(r["lon"], False)))
        for r in read("routes"):
            con.execute("INSERT INTO routes VALUES (?,?,?,?,?,?,?)", (r["route_id"], r["origin"], r["destination"], r["mode"], r["carrier_id"], num(r["transit_days"]), r.get("via")))

    def suppliers():
        for folder in sorted(p for p in (current / paths["supplier_root"]).iterdir() if p.is_dir()):
            prof = folder / paths["supplier_profile"]
            if not prof.exists():
                issue(str(folder), f"no {prof.name}")
                continue
            comps = folder / paths["supplier_components"]
            sid = customer.supplier_id(sources, folder.name)
            for r in read("supplier_components", comps) if comps.exists() else []:
                con.execute("INSERT INTO supplier_components VALUES (?,?,?,?,?,?,?,?,?,?)", (sid, r["component_id"], r.get("description"),
                            num(r.get("share"), False), num(r.get("unit_price"), False), r.get("currency"), num(r.get("lead_time_days")),
                            num(r.get("moq")), r.get("primary"), r.get("note")))
            for r in read("supplier_profile", prof):
                con.execute("INSERT OR REPLACE INTO suppliers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (r["supplier_id"], r["name"], r["country"], r["city"],
                            r.get("incoterm"), r.get("status"), r.get("qualification_status"), r.get("contact_name"), r.get("contact_email"),
                            r.get("phone"), r.get("preferred_language"), folder.name))
                for f in ("status", "qualification_status", "incoterm", "contact_email"):
                    obs("supplier", r["supplier_id"], f, r.get(f), str(prof.relative_to(current)))
            pj = folder / paths["supplier_profile_json"] if paths.get("supplier_profile_json") else None
            if pj and pj.exists():
                d = json.loads(pj.read_text())
                sp = d.get("supplier", d)
                f = flatten(sp)
                sid = f.get("supplierId") or sid
                rel = str(pj.relative_to(current))
                as_of = d.get("exportedAt")
                mapping = {"status": f.get("status"), "qualification_status": f.get("qualification.status"), "incoterm": f.get("terms.incoterm"),
                           "contact_email": f.get("contact.email"), "contact_name": f.get("contact.name"), "phone": f.get("contact.phone"),
                           "language": f.get("contact.language")}
                for k, v in mapping.items():
                    if v is not None:
                        con.execute(f"UPDATE suppliers SET {k}=? WHERE supplier_id=?", (v, sid))
                        if k in ("status", "qualification_status", "incoterm", "contact_email"):
                            obs("supplier", sid, k, v, rel, as_of)
                for k in ("qualification.validUntil", "qualification.extendedBy", "notes"):
                    if f.get(k) is not None:
                        obs("supplier", sid, k, f.get(k), rel, as_of)
            for dfile in matches("supplier_deltas", folder):
                for r in read("supplier_deltas", dfile):
                    rel = str(dfile.relative_to(current))
                    for fld in ("lead_time_days", "share", "primary"):
                        new = r.get(f"{fld}_new") if f"{fld}_new" in r else r.get(fld)
                        if new not in (None, ""):
                            col = "is_primary" if fld == "primary" else fld
                            old = con.execute(f"SELECT {col} FROM supplier_components WHERE supplier_id=? AND component_id=?", (sid, r["component_id"])).fetchone()
                            if old is not None and str(old[0]) != str(num(new) if fld != "primary" else new):
                                obs("supplier_component", f"{sid}/{r['component_id']}", fld, old[0], str(comps.relative_to(current)))
                                obs("supplier_component", f"{sid}/{r['component_id']}", fld, new, rel)
                            con.execute(f"UPDATE supplier_components SET {col}=? WHERE supplier_id=? AND component_id=?",
                                        (num(new) if fld != "primary" else new, sid, r["component_id"]))
                    if r.get("note"):
                        con.execute("UPDATE supplier_components SET note=trim(coalesce(note,'') || ' | ' || ?) WHERE supplier_id=? AND component_id=?",
                                    (f"{r.get('action', 'note')}: {r['note']}", sid, r["component_id"]))

    def pos():
        legacy = sources.get("legacy_sap_po")
        for f in matches("purchase_orders"):
            rel = str(f.relative_to(current))
            for r in read("purchase_orders", f):
                if legacy and "EBELN" in r:  # legacy SAP export: German decimals, US dates, bare supplier numbers
                    sup = r["LIFNR"].strip()
                    sup = legacy["supplier_id_format"].format(number=int(sup)) if sup.isdigit() else sup
                    line = int(r["EBELP"]) // legacy["line_divisor"] if r["EBELP"].isdigit() else r["EBELP"]
                    row = (r["EBELN"], line, sup, r["MATNR"], r["WERKS"], num(r["MENGE"], True), num(r["NETPR"], True), None,
                           date_iso(r["BEDAT"]), date_iso(r["EINDT"]), date_iso(r.get("ZZCONF")), date_iso(r.get("WEDAT")),
                           legacy["status_codes"].get(r.get("ZZSTAT"), r.get("ZZSTAT")), r.get("ERNAM"), None, None, rel)
                else:
                    row = (r["po_number"], num(r["po_line"]), r["supplier_id"], r["material_id"], r["plant"], num(r["quantity"], False),
                           num(r["unit_price"], False), r.get("currency"), r.get("order_date"), r.get("requested_date"), r.get("promised_date"),
                           r.get("actual_delivery_date") or None, r.get("status"), r.get("buyer"), r.get("incoterm"), r.get("note"), rel)
                con.execute("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)

    def receipts():
        for r in read("receipts"):
            con.execute("INSERT OR REPLACE INTO goods_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)", (r["gr_number"], r["gr_date"], r["po_number"],
                        num(r["po_line"]), r["supplier_id"], r["material_id"], num(r["qty_ordered"], False), num(r["qty_received"], False),
                        r.get("delivery_note"), r.get("inspector"), r.get("remark")))
        for r in read("stock"):
            con.execute("INSERT INTO stock VALUES (?,?,?,?,?)", (r["snapshot_date"], r["material_id"], r.get("batch") or None,
                        num(r["quantity"], False), r.get("last_count")))

    def shipments():
        rel = paths["shipments"]
        for r in read("shipments"):
            quantity = num(r.get("quantity"), False)
            con.execute("INSERT INTO shipments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (r["shipment_id"], r["carrier_id"], r["mode"],
                        r["reference"], r["supplier_id"], r["po_numbers"], r["material_ids"], r["route_id"], r["origin"], r["destination"],
                        r.get("via"), r.get("dest_site") or rules.setting("site"), r["etd"], r["eta"], r["status"], num(r.get("last_lat"), False),
                        num(r.get("last_lon"), False), r["updated_at"], r.get("note"), quantity))
            for fld in ("status", "eta", "etd"):
                obs("shipment", r["shipment_id"], fld, r[fld].replace("_", " "), rel, r["updated_at"])
            if quantity is not None:
                obs("shipment", r["shipment_id"], "quantity", quantity, rel, r["updated_at"])
        by_ref = {r[0]: r[1] for r in con.execute("SELECT reference, shipment_id FROM shipments")}
        for f in matches("shipment_tracker"):
            rel = str(f.relative_to(current))
            for sheet, rows in xlsx_sheets(f).items():
                for r in workbook_rows("shipment_tracker", rows, "shipment_id"):
                    sid = r.get("shipment_id") or by_ref.get(r.get("reference"))
                    if not sid:
                        continue
                    for fld in ("status", "eta", "etd"):
                        value = date_iso(r.get(fld)) if fld != "status" else r.get(fld)
                        obs("shipment", sid, fld, value, f"{rel}#{sheet}", None, r.get("note"))

        sheet = sources.get("inbound_sheet")
        for f in matches("inbound_plan"):
            rel = str(f.relative_to(current))
            if f.suffix.lower() == ".xlsx":
                rows = workbook_rows("inbound_plan", xlsx_sheets(f).get(sheet, []), "reference")
                rel = f"{rel}#{sheet}"
            else:
                rows = read("inbound_plan", f)
            for r in rows:
                sid = by_ref.get(str(r.get("reference")))
                if not sid:
                    issue(rel, f"inbound reference not in ERP shipments: {r.get('reference')}")
                    continue
                quantity = num(r.get("quantity"))
                obs("shipment", sid, "quantity", quantity, rel, None, r.get("material_id"))
                obs("shipment", sid, "status", r.get("status"), rel)
                obs("shipment", sid, "eta", date_iso(r.get("eta")), rel)
                if quantity is not None:
                    con.execute("UPDATE shipments SET quantity=COALESCE(quantity, ?) WHERE shipment_id=?", (quantity, sid))

    def launch():
        """Read launch status for product IDs in the master catalog."""
        product_ids = {pid for pid, in con.execute("SELECT product_id FROM products")}

        def record(body, rel, default_as_of=None):
            for r in body:
                pid = r.get("product_id")
                if pid not in product_ids:
                    continue
                as_of = parse_as_of(r["as_of"]) if r.get("as_of") else default_as_of
                if not as_of:
                    issue(rel, f"launch status for {pid} has no recognized timestamp")
                    continue
                packed = num(r.get("packed"))
                con.execute("INSERT INTO launch_status VALUES (?,?,?,?,?,?,?)", (pid, as_of, num(r.get("demand")), packed,
                            num(r.get("still_to_pack")), num(r.get("rate_per_hour")), rel))
                obs("launch", pid, "packed", packed, rel, as_of)

        for f in matches("launch"):
            rel = str(f.relative_to(current))
            if f.suffix.lower() == ".xlsx":
                for sheet, rows in xlsx_sheets(f).items():
                    if rows:
                        title = next((str(c) for c in rows[0] if c), "")
                        record(workbook_rows("launch", rows, "product_id"), f"{rel}#{sheet}", parse_as_of(title))
            else:
                record(read("launch", f), rel)

    def documents():
        if news := paths.get("news"):
            for p in sorted((current / news).rglob("*")):
                if p.is_file():
                    con.execute("INSERT INTO documents VALUES (?,?,?,?,?)", (str(p.relative_to(current)), None, "news", None, p.read_text(errors="replace")))
        supplier_root = current / paths["supplier_root"]
        supplier_docs = sorted(supplier_root.rglob("*.pdf")) + sorted(
            p for p in supplier_root.rglob("*") if p.is_file() and p.suffix.lower() in (".txt", ".md", ".eml"))
        receiving_docs = sorted((current / paths["receiving"]).rglob("*.pdf")) if paths.get("receiving") else []
        for p in supplier_docs + receiving_docs:
            rel = str(p.relative_to(current))
            sup = customer.supplier_id(sources, p.relative_to(supplier_root).parts[0]) if p.is_relative_to(supplier_root) else None
            pattern = sources.get("document_po_pattern")
            match = re.search(pattern, p.name) if pattern else None
            body = pdf_text(p) if p.suffix.lower() == ".pdf" else p.read_text(errors="replace")
            con.execute("INSERT INTO documents VALUES (?,?,?,?,?)", (rel, sup, p.parent.name, match.group(0) if match else None, body))
        for jf in sorted(facts_dir.glob("*.json")):
            d = json.loads(jf.read_text())
            for fact in d.get("facts", []):
                con.execute("INSERT INTO doc_facts VALUES (?,?,?,?)", (d["path"], fact.get("type"), json.dumps(fact, ensure_ascii=False), d.get("extracted_by")))

    for fn in (master, suppliers, pos, receipts, shipments, launch, documents):
        safe(fn)
    con.executemany("INSERT INTO ingest_issues VALUES (?,?)", issues)
    con.commit()
    con.close()
    tmp.replace(db_path)
    return {"files": len(files), "issues": len(issues)}


def parse_as_of(title: str) -> str | None:
    """'... status 2026-11-20 18:00 ...' or '... · Saturday 13:00 · ...' -> ISO timestamp (launch week)."""
    deadline = rules.ts(rules.cfg("deadline"))
    tz = deadline.tzinfo if deadline else rules.timezone()
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})", title)
    if m:
        return dt.datetime.fromisoformat(f"{m.group(1)}T{int(m.group(2).split(':')[0]):02d}:{m.group(2).split(':')[1]}").replace(tzinfo=tz).isoformat()
    m = re.search(r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(\d{1,2}):(\d{2})", title, re.I)
    if m and deadline:
        # A weekday in a status title means the last such day on or before the deadline (the launch week).
        back = (deadline.weekday() - WEEKDAYS.index(m.group(1).lower())) % 7
        day = (deadline - dt.timedelta(days=back)).date()
        return dt.datetime(day.year, day.month, day.day, int(m.group(2)), int(m.group(3)), tzinfo=tz).isoformat()
    return None


if __name__ == "__main__":
    print(build())
