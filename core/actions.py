"""Action log: everything the agents propose, who must approve it, what was sent, and when to follow up.

Lives in data/actions.db, which is NOT rebuilt (unlike the master DB): it is the system's memory.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path

from core.rules import TZ, approver_for, cfg, now

DB = Path(__file__).resolve().parent.parent / "data" / "actions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions(
  id TEXT PRIMARY KEY, finding_id TEXT, title TEXT, rationale TEXT, kind TEXT,
  cost_eur REAL, eur_protected REAL, ce_copies INTEGER, category TEXT,
  approver_role TEXT, approver_name TEXT, owner TEXT,
  status TEXT,            -- proposed | approved | rejected | sent | done | escalated | superseded
  created_at TEXT, updated_at TEXT, due_at TEXT, next_check_at TEXT, escalation_level INTEGER DEFAULT 0,
  payload TEXT, history TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, action_id TEXT, channel TEXT, to_name TEXT, to_address TEXT, language TEXT,
  subject TEXT, body TEXT, body_en TEXT, status TEXT, created_at TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS events(ts TEXT, kind TEXT, ref TEXT, text TEXT);
"""


def con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def log_event(kind: str, ref: str | None, text: str) -> None:
    with con() as c:
        c.execute("INSERT INTO events VALUES (?,?,?,?)", (now().isoformat(timespec="seconds"), kind, ref, text))


def _hist(row, entry: str) -> str:
    h = json.loads(row["history"] or "[]") if row else []
    h.append({"ts": now().isoformat(timespec="seconds"), "entry": entry})
    return json.dumps(h, ensure_ascii=False)


def open_action_for(finding_id: str, kind: str | None = None) -> dict | None:
    with con() as c:
        q = "SELECT * FROM actions WHERE finding_id=? AND status IN ('proposed','approved','sent','escalated','auto')"
        args = [finding_id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        r = c.execute(q + " ORDER BY created_at DESC LIMIT 1", args).fetchone()
    return dict(r) if r else None


def propose(finding_id: str, title: str, rationale: str, kind: str, cost_eur: float, eur_protected: float, ce_copies: int,
            category: str | None, owner: str | None, due_in_minutes: int = 60, payload: dict | None = None) -> dict:
    ap = approver_for(cost_eur, category)
    special = cfg("approval_by_kind").get(kind)
    if special:  # non-monetary sign-offs (quality concession, dock slot) follow their own rule, not the cost ladder
        people, names = cfg("people"), []
        for role in special:
            name = people.get(f"{role}:{category}") if role == "Category Lead" else people.get(role)
            if role == "Category Lead" and not name:
                role, name = "Head of Procurement", people.get("Head of Procurement")  # vacant seat -> next tier
            names.append((role, name))
        if cost_eur and approver_for(cost_eur, category).get("role") not in [r for r, _ in names]:
            names.append((ap.get("role"), ap.get("name")))
        ap = {"role": " + ".join(r for r, _ in names), "name": " + ".join(n for _, n in names)}
    t = now()
    aid = "ACT-" + uuid.uuid4().hex[:6].upper()
    with con() as c:
        c.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            aid, finding_id, title, rationale, kind, cost_eur, eur_protected, ce_copies, category, ap.get("role"), ap.get("name"), owner,
            "proposed", t.isoformat(timespec="seconds"), t.isoformat(timespec="seconds"),
            (t + dt.timedelta(minutes=due_in_minutes)).isoformat(timespec="seconds"),
            (t + dt.timedelta(minutes=min(due_in_minutes, 30))).isoformat(timespec="seconds"), 0,
            json.dumps(payload or {}, ensure_ascii=False), json.dumps([{"ts": t.isoformat(timespec="seconds"), "entry": f"proposed; needs {ap.get('role')} ({ap.get('name')})" + (f" — {ap['note']}" if ap.get("note") else "")}])))
    log_event("action_proposed", aid, title)
    return get(aid)


def add_message(action_id: str, channel: str, to_name: str, to_address: str | None, language: str, subject: str, body: str, body_en: str | None = None) -> str:
    mid = "MSG-" + uuid.uuid4().hex[:6].upper()
    with con() as c:
        c.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (mid, action_id, channel, to_name, to_address, language, subject, body,
                  body_en, "draft", now().isoformat(timespec="seconds"), None))
    return mid


def get(aid: str) -> dict | None:
    with con() as c:
        r = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"] or "{}")
        d["history"] = json.loads(d["history"] or "[]")
        d["messages"] = [dict(m) for m in c.execute("SELECT * FROM messages WHERE action_id=? ORDER BY created_at", (aid,))]
    return d


def list_actions(statuses: tuple = ("proposed", "approved", "sent", "escalated", "done", "rejected", "auto")) -> list[dict]:
    with con() as c:
        ids = [r[0] for r in c.execute(f"SELECT id FROM actions WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY eur_protected DESC, created_at",
                                       statuses)]
    return [get(i) for i in ids]


def decide(aid: str, decision: str, by: str, note: str = "") -> dict:
    """Human decision from the web UI or Discord. approve -> messages become sendable."""
    assert decision in ("approve", "reject")
    status = "approved" if decision == "approve" else "rejected"
    with con() as c:
        r = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
        if not r:
            raise KeyError(aid)
        if r["status"] not in ("proposed", "escalated"):
            raise ValueError(f"{aid} is already {r['status']}")
        c.execute("UPDATE actions SET status=?, updated_at=?, history=? WHERE id=?",
                  (status, now().isoformat(timespec="seconds"), _hist(r, f"{status} by {by}" + (f": {note}" if note else "")), aid))
    log_event(f"action_{status}", aid, f"{by}: {note}")
    return get(aid)


def set_status(aid: str, status: str, entry: str, **fields) -> dict:
    with con() as c:
        r = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE actions SET status=?, updated_at=?, history=?{', ' + sets if sets else ''} WHERE id=?",
                  (status, now().isoformat(timespec="seconds"), _hist(r, entry), *fields.values(), aid))
    log_event(f"action_{status}", aid, entry)
    return get(aid)


def due_followups() -> list[dict]:
    """Proposed actions nobody decided on in time -> escalate one tier up the ladder."""
    t = now().isoformat(timespec="seconds")
    with con() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM actions WHERE status IN ('proposed','escalated','sent') AND next_check_at <= ?", (t,))]
    return [get(i) for i in ids]


def escalate(a: dict) -> dict:
    ladder = cfg("approval_ladder")
    people = cfg("people")
    roles = [x["role"] for x in ladder]
    idx = roles.index(a["approver_role"]) if a["approver_role"] in roles else 0
    nxt = roles[min(idx + 1, len(roles) - 1)]
    name = people.get(nxt)
    t = now()
    return set_status(a["id"], "escalated", f"no decision from {a['approver_name']} by {a['next_check_at']}; escalated to {nxt} ({name})",
                      approver_role=nxt, approver_name=name, escalation_level=a["escalation_level"] + 1,
                      next_check_at=(t + dt.timedelta(minutes=30)).isoformat(timespec="seconds"))
