#!/usr/bin/env python3
"""Replay a reviewed customer mapping and successive file drops in a scratch database."""
from __future__ import annotations

import argparse
import datetime as dt
import filecmp
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import customer, dispatch, ingest, rules


def apply_drop(source: Path, current: Path) -> list[str]:
    changed = []
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        relative = path.relative_to(source)
        target = current / relative
        if target.exists() and filecmp.cmp(path, target, shallow=False):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        changed.append(relative.as_posix())
    return changed


def run_customer(folder: Path, through: int | None = None) -> list[dict]:
    config = customer.load(folder / "business.json")
    at = dt.datetime.fromisoformat(config["replay_at"]["value"])
    drops = [(1, folder / "raw")]
    drop_root = folder / "drops"
    if drop_root.exists():
        drops += sorted((int(p.name), p) for p in drop_root.iterdir() if p.is_dir())
    if through is not None and through not in dict(drops):
        raise ValueError(f"No drop {through} in {folder}")

    reports = []
    with TemporaryDirectory(prefix="dockside-replay-") as tmp, rules.replay(config, at):
        db = Path(tmp) / "customer.db"
        current = Path(tmp) / "current"
        for number, source in drops:
            if through is not None and number > through:
                break
            changed = apply_drop(source, current)
            built = ingest.build(db, current, facts_dir=folder / "doc_facts")
            reports.append(dict(customer=rules.cfg("customer_name"), drop=number, changed=changed, build=built,
                                evaluation=rules.evaluate(db), dispatch=dispatch.snapshot(None, db_path=db, include_activity=False)))
    return reports


def print_report(report: dict) -> None:
    snapshot = report["dispatch"]
    findings = report["evaluation"]["findings"]
    parts = {p["id"]: p for p in snapshot["components"]}
    focus = parts.get(snapshot["focusComponentId"])
    print(f"Drop {report['drop']} | {report['customer']} | {snapshot['siteName']} | deadline {snapshot['deadline']}")
    print(f"  Files changed {len(report['changed'])}; ingest issues {report['build']['issues']}")
    for edition in snapshot["editions"]:
        print(f"  {edition['name']} ({edition['id']}): {edition['projected']:,}/{edition['demand']:,} projected, "
              f"{edition['short']:,} short, EUR {edition['refundsAtRisk']:,.0f} at risk")
    if focus:
        print(f"  Biggest lever: {focus['name']} ({focus['id']}), {focus['usable']:,}/{focus['required']:,} usable")
        print(f"  Next step [{focus['next_step']['state']}]: {focus['next_step']['text']}")
    else:
        print("  Biggest lever: packing capacity" if any(e["capacityLimited"] for e in snapshot["editions"]) else "  No part shortage")
    for shipment in report["evaluation"]["inbound"]:
        if shipment["quantity_review"]:
            state = "held for review"
        elif shipment["makes_cutoff"] is True:
            state = "by cutoff"
        elif shipment["makes_cutoff"] is False:
            state = "after cutoff"
        else:
            state = "arrival unconfirmed"
        qty = f"{shipment['quantity']:g} units" if shipment["quantity"] is not None else "quantity unconfirmed"
        print(f"  Inbound {shipment['reference']}: {qty}; {state}")
        for flag in shipment["flags"]:
            if flag.startswith("supplier hold"):
                print(f"    {flag}")
    print("  Findings:")
    for finding in findings:
        print(f"    [{finding['severity']}] {finding['title']}")
        if finding["kind"] == "review":
            print(f"      {finding['detail']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--through", type=int, help="stop after this drop")
    args = parser.parse_args()
    try:
        for report in run_customer(args.folder, args.through):
            print_report(report)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
