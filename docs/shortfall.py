"""GH6 launch component shortfall vs inbound shipments. Read-only on source data.
Usage: python3 -I shortfall.py [DATA_ROOT]"""
import csv, sys, os, re, zipfile, xml.etree.ElementTree as ET
from collections import defaultdict, Counter
from datetime import datetime, timedelta, date

D = sys.argv[1] if len(sys.argv) > 1 else '/home/emu/Projects/SooneyAgents/data/snapshot/soony-data/soony'
P = lambda *a: os.path.join(D, *a)
def rd(path, delim=','):
    with open(path, newline='', encoding='utf-8') as f: return list(csv.DictReader(f, delimiter=delim))

# ---------- master ----------
bom = rd(P('master/bom.csv')); delta = rd(P('master/bom_delta_2026-11-21.csv'))
comps = {r['component_id']: r for r in rd(P('master/components.csv'))}
for u in rd(P('master/components_update_2026-11-21.csv')):
    c = comps[u['component_id']]
    assert c[u['field']] == u['old'], u
    if u['old'] == u['new']: print('NOTE no-op update:', u)
    c[u['field']] = u['new']
routes = {r['route_id']: r for r in rd(P('master/routes.csv'))}
print('BOM delta:'); [print('  ', d['change_id'], d['parent_id'], d['child_id'], d['rev_old'], '->', d['rev_new'], 'use_up_old=', d['use_up_old_rev'], '|', d['reason']) for d in delta]
blocked_rev = {d['child_id']: d['rev_old'] for d in delta if d['use_up_old_rev'] == 'N'}

# ---------- demand ----------
DEMAND = {'FG-GH6-STD': 640000, 'FG-GH6-CE': 72000}
stock = rd(P('erp/stock_snapshot.csv'))
fg = {r['material_id']: int(r['quantity']) for r in stock if r['material_id'].startswith('FG')}
remaining = {p: DEMAND[p] - fg[p] for p in DEMAND}
# launch_status.xlsx (Sat 13:00): STD packed 554,000, CE 53,100 -> consumption since snapshot
SAT13 = {'FG-GH6-STD': 554000, 'FG-GH6-CE': 53100}
since = {p: SAT13[p] - fg[p] for p in DEMAND}
print('remaining at kickoff', remaining, 'packed since snapshot (Sat 13:00)', since)
SCRAP = 0.0   # no scrap factor in any master file; set e.g. 0.01 for sensitivity

req = defaultdict(float); used_since = defaultdict(int)
for b in bom:
    if b['parent_id'] in DEMAND and b['valid_to'] >= '2026-11-21':
        q = float(b['qty_per'])
        req[b['child_id']] += remaining[b['parent_id']] * q * (1 + SCRAP)
        used_since[b['child_id']] += int(since[b['parent_id']] * q)

# ---------- stock + reality adjustments ----------
onhand = defaultdict(int); batches = defaultdict(list)
for r in stock:
    if r['site'] != 'DC-HAM' or r['material_id'].startswith('FG'): continue
    onhand[r['material_id']] += int(r['quantity']); batches[r['material_id']].append((r['batch'], int(r['quantity'])))
gr = rd(P('erp/goods_receipts.csv'))
grb = {g['gr_number']: g for g in gr}
adj = defaultdict(list)   # (qty, reason)
# 1) over-receipt typo: GR qty 10x ordered (delivery note WZ-261116-473 hand-corrected 80.000 -> 8 000, 360 kg/40 pk)
for g in gr:
    if g['qty_ordered'] and g['qty_received'] and int(g['qty_received']) >= 5 * int(g['qty_ordered']):
        adj[g['material_id']].append((-(int(g['qty_received']) - int(g['qty_ordered'])), f"GR {g['gr_number']} booked {g['qty_received']} vs ordered {g['qty_ordered']} (DN corrected to {g['qty_ordered']})"))
# 2) quality hold: inspection_report_5000413063.pdf -> lot rejected (AQL), booked unrestricted
for m, bl in batches.items():
    for b, q in bl:
        if b == '5000413063': adj[m].append((-q, f'batch {b} failed AQL 1.0 (9/125 defects), blocking requested'))
# 3) superseded revision with no use-up: all stock received before ECN is old rev
for m, rev in blocked_rev.items():
    adj[m].append((-onhand[m], f'all stock is rev {rev}; ECN forbids packing rev {rev}'))

# ---------- POs: reconcile two SAP exports ----------
pos = {}
dups = []
def put(k, v, src):
    if k in pos: dups.append((k, src))
    pos[k] = v
for r in rd(P('erp/purchase_orders_legacy_2024-2025H1.csv'), ';'):
    k = (r['EBELN'], int(r['EBELP']) // 10)
    md = lambda s: datetime.strptime(s, '%m/%d/%Y').date().isoformat() if s else ''
    put(k, dict(material=r['MATNR'], qty=int(r['MENGE']), price=float(r['NETPR'].replace(',', '.')),
                supplier='SUP-%04d' % int(r['LIFNR']), status={'GL': 'delivered', 'LO': 'cancelled'}.get(r['ZZSTAT'], r['ZZSTAT']),
                promised=md(r['ZZCONF']), order=md(r['BEDAT'])), 'legacy')
for r in rd(P('erp/purchase_orders_2025H2-2026.csv')):
    put((r['po_number'], int(r['po_line'])), dict(material=r['material_id'], qty=int(r['quantity']), price=float(r['unit_price']),
        supplier=r['supplier_id'], status=r['status'], promised=r['promised_date'], order=r['order_date']), 'new')
print('PO lines', len(pos), 'duplicate keys', dups)
received = defaultdict(int)
for g in gr:
    if g['po_number']: received[(g['po_number'], int(g['po_line']))] += int(g['qty_received'])
def open_lines(po, mats):
    return [(po, l, v['material'], v['qty'] - received.get((po, l), 0)) for (p, l), v in sorted(pos.items())
            if p == po and v['status'] == 'open' and v['material'] in mats]

# ---------- shipments ----------
ship = rd(P('erp/open_shipments.csv'))
def read_xlsx(path):
    M = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    z = zipfile.ZipFile(path); rows = []
    for row in ET.fromstring(z.read('xl/worksheets/sheet1.xml')).iter(M + 'row'):
        d = {}
        for c in row.findall(M + 'c'):
            col = re.match(r'[A-Z]+', c.get('r')).group()
            d[col] = ''.join(x.text or '' for x in c.iter(M + 't')) if c.get('t') == 'inlineStr' else (c.find(M + 'v').text if c.find(M + 'v') is not None else '')
        rows.append(d)
    hdr = rows[0]; return [{hdr[k]: v for k, v in r.items()} for r in rows[1:]]
trk = {r['Shipment']: r for r in read_xlsx(P('logistics/open_shipments_tracker_v37_FINAL_final2.xlsx'))}

CUTOFF = datetime(2026, 11, 22, 16, 0)
def dc_arrival(s):
    eta = datetime.fromisoformat(s['eta']); r = routes[s['route_id']]
    if r['mode'] in ('sea', 'air'):
        last = [x for x in routes.values() if x['origin'] == r['destination'] and x['destination'] == 'DC-HAM'][0]
        eta += timedelta(days=int(last['transit_days']))
    return eta
gap_comps = set()
rows_out = []
for s in ship:
    mats = s['material_ids'].split('|'); pl = []
    for po in s['po_numbers'].split('|'): pl += open_lines(po, mats)
    arr = dc_arrival(s)
    rows_out.append((s['shipment_id'], s['mode'], s['status'], s['eta'], arr.date(), arr <= CUTOFF, pl))

# ---------- component table ----------
print('\n%-12s %9s %9s %9s %9s %9s  %s' % ('component', 'need', 'onhand', 'adj', 'usable', 'gap', 'inbound-before-cutoff'))
for m in sorted(req):
    a = sum(q for q, _ in adj[m]); usable = onhand[m] + a; gap = usable - req[m]
    inb = [(sid, sum(q for _, _, mm, q in pl if mm == m)) for sid, _, _, _, _, ok, pl in rows_out if ok and any(mm == m for _, _, mm, _ in pl)]
    print('%-12s %9d %9d %9d %9d %9d  %s' % (m, req[m], onhand[m], a, usable, gap, inb))
    for q, why in adj[m]: print('    adj %+d: %s' % (q, why))
    print('    packed since snapshot (Sat 13:00) consumes %d -> est. physical left %d' % (used_since[m], onhand[m] - used_since[m]))

print('\nShipments (ERP csv):')
for sid, mode, st, eta, arr, ok, pl in rows_out:
    print(f"  {sid} {mode:4} {st:12} ETA {eta} -> DC {arr} {'BEFORE' if ok else 'late  '} {[(p, l, m, q) for p, l, m, q in pl]}")

print('\nContradictions ERP vs tracker:')
norm = lambda x: x.replace('_', ' ').strip().lower()
for s in ship:
    t = trk.get(s['shipment_id'])
    if not t: print(f"  {s['shipment_id']} only in ERP ({s['mode']}, {s['status']}, ETA {s['eta']}, {s['material_ids']})"); continue
    for ef, tf in [('reference', 'Container/AWB'), ('etd', 'ETD'), ('eta', 'ETA'), ('origin', 'From'), ('carrier_id', 'Carrier')]:
        if s[ef] != t[tf]: print(f"  {s['shipment_id']} {ef}: ERP={s[ef]} tracker={t[tf]}")
    if norm(s['status']) != norm(t['Status']): print(f"  {s['shipment_id']} status: ERP={s['status']} tracker={t['Status']} (ERP note: {s['note']!r}; tracker comment: {t['Comment']!r})")
    if set(s['po_numbers'].split('|')) != set(x.strip() for x in t['POs'].split(',')): print(f"  {s['shipment_id']} POs differ: {s['po_numbers']} vs {t['POs']}")
for k in trk:
    if k not in {s['shipment_id'] for s in ship}: print('  only in tracker:', k)
