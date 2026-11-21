"""Deterministic logistics math the agent calls as tools: truck legs, Sunday ban, cut-off, what-if."""
from __future__ import annotations

import copy
import datetime as dt

from core.rules import TZ, cfg, now, ts


def next_shift_minute(t: dt.datetime) -> dt.datetime:
    """First moment at or after t when the DC is working (receiving follows the pack shifts)."""
    for s in cfg("shifts"):
        st, en = ts(s["start"]), ts(s["end"])
        if t < en:
            return max(t, st)
    return t


def plan_truck(ready_at: str | None, drive_hours: float, from_port: bool = False, vehicle_t: float = 40,
               load_hours: float = 0, origin: str = "") -> dict:
    """Earliest arrival at DC-HAM for a truck leg, honouring the German Sunday ban and the DC shifts."""
    ban = cfg("truck_sunday_ban")
    deadline = ts(cfg("deadline"))
    cutoff = deadline - dt.timedelta(hours=cfg("receiving_buffer_hours"))
    depart = (ts(ready_at) if ready_at else now()) + dt.timedelta(hours=load_hours)
    arrive = depart + dt.timedelta(hours=drive_hours)
    notes = []
    banned = vehicle_t > ban["min_weight_t"] and not from_port
    if banned:
        # Ban window on the launch Sunday (00:00-22:00): a heavy truck must stop at the border / a lay-by.
        sun = deadline.replace(hour=0, minute=0)
        ban_end = sun.replace(hour=22)
        if arrive > sun and depart < ban_end:
            notes.append(f"Sunday ban: a {vehicle_t:g} t truck may not drive in DE Sun 00:00-22:00 -> arrives Sun 22:00+ (after the 20:00 deadline)")
            arrive = ban_end + max(dt.timedelta(0), arrive - max(depart, sun))
    elif vehicle_t <= ban["min_weight_t"]:
        notes.append(f"vehicle <= {ban['min_weight_t']} t: Sunday ban does not apply")
    elif from_port:
        notes.append("port-road combined transport within 150 km: exempt from the Sunday ban")
    unload = next_shift_minute(arrive)
    if unload > arrive:
        notes.append(f"DC closed on arrival; unloading at {unload:%a %H:%M}")
    return dict(origin=origin, depart=depart.isoformat(timespec="minutes"), arrive=arrive.isoformat(timespec="minutes"),
                unload=unload.isoformat(timespec="minutes"), cutoff=cutoff.isoformat(timespec="minutes"),
                makes_cutoff=unload <= cutoff, hours_before_deadline=round((deadline - unload).total_seconds() / 3600, 1), notes=notes)


def what_if(shipment_id: str | None = None, delay_hours: float = 0, qty: float | None = None, drop: bool = False) -> dict:
    """Re-run the rules engine with one shipment changed; return the delta in CE copies / EUR."""
    from core import rules
    base = rules.evaluate()
    orig_inbound = rules.inbound

    def patched(con, coverage):
        out = orig_inbound(con, coverage)
        for s in out:
            if s["shipment_id"] == shipment_id:
                if drop:
                    s["arrival_at_dc"], s["makes_cutoff"] = None, False
                if delay_hours and s["arrival_at_dc"]:
                    a = dt.datetime.fromisoformat(s["arrival_at_dc"]) + dt.timedelta(hours=delay_hours)
                    s["arrival_at_dc"] = a.isoformat()
                    s["makes_cutoff"] = a <= dt.datetime.fromisoformat(s["cutoff"])
                if qty is not None:
                    s["quantity"] = qty
        return out

    rules.inbound = patched
    try:
        alt = rules.evaluate()
    finally:
        rules.inbound = orig_inbound
    pick = lambda r: next(f for f in r["findings"] if f["id"] == "ce-at-risk")
    sb = next((s for s in alt["inbound"] if s["shipment_id"] == shipment_id), None)
    return dict(shipment=sb, ce_at_risk_before=pick(base)["ce_copies"], ce_at_risk_after=pick(alt)["ce_copies"],
                eur_before=pick(base)["eur_at_risk"], eur_after=pick(alt)["eur_at_risk"])
