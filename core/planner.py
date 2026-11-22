"""Deterministic logistics math the agent calls as tools: truck legs, Sunday ban, cut-off, what-if."""
from __future__ import annotations

import datetime as dt

from core.rules import cfg, now, ts


def next_shift_minute(t: dt.datetime) -> dt.datetime:
    """First moment at or after t when the DC is working (receiving follows the pack shifts)."""
    for s in cfg("shifts"):
        st, en = ts(s["start"]), ts(s["end"])
        if t < en:
            return max(t, st)
    return t


def plan_truck(ready_at: str | None, drive_hours: float, from_port: bool = False, vehicle_t: float = 40,
               load_hours: float = 0, origin: str = "") -> dict:
    """Earliest arrival at the configured site given driving restrictions and receiving shifts."""
    ban = cfg("truck_sunday_ban")
    deadline = ts(cfg("deadline"))
    cutoff = deadline - dt.timedelta(hours=cfg("receiving_buffer_hours"))
    depart = (ts(ready_at) if ready_at else now()) + dt.timedelta(hours=load_hours)
    arrive = depart + dt.timedelta(hours=drive_hours)
    notes = []
    if ban:
        if vehicle_t <= ban["min_weight_t"]:
            notes.append(f"Vehicle <= {ban['min_weight_t']} t is exempt from the truck ban.")
        elif from_port:
            notes.append(f"Port-road combined transport within {ban['port_exempt_km']} km is exempt from the truck ban.")
        else:
            day = depart.date() + dt.timedelta(days=(ban["weekday"] - depart.weekday()) % 7)
            start_time, end_time = (dt.time.fromisoformat(ban[key]) for key in ("from", "to"))
            start = dt.datetime.combine(day, start_time, depart.tzinfo)
            while start < arrive:
                end = dt.datetime.combine(start.date(), end_time, depart.tzinfo)
                if depart < end:
                    notes.append(f"{ban['country']} truck ban on {start:%A %H:%M}–{end:%H:%M} applies to a {vehicle_t:g} t truck.")
                    arrive += end - max(depart, start)
                start += dt.timedelta(days=7)
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
