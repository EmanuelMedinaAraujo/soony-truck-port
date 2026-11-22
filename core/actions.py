"""Action log: everything the agents propose, who must approve it, what was sent, and when to follow up.

Lives in data/actions.db, which is NOT rebuilt (unlike the master DB): it is the system's memory.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from collections.abc import Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path

from core.rules import approver_for, cfg, now, ts

DB = Path(__file__).resolve().parent.parent / "data" / "actions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions(
  id TEXT PRIMARY KEY, finding_id TEXT, title TEXT, rationale TEXT, kind TEXT,
  cost_eur REAL, eur_protected REAL, ce_copies INTEGER, category TEXT,
  approver_role TEXT, approver_name TEXT, owner TEXT,
  status TEXT,            -- proposed | scheduled | approved | rejected | sent | done | escalated | auto
  created_at TEXT, updated_at TEXT, due_at TEXT, next_check_at TEXT, escalation_level INTEGER DEFAULT 0,
  payload TEXT, history TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, action_id TEXT, channel TEXT, to_name TEXT, to_address TEXT, language TEXT,
  subject TEXT, body TEXT, body_en TEXT, status TEXT, created_at TEXT, sent_at TEXT, provider_id TEXT,
  po_numbers TEXT);
CREATE TABLE IF NOT EXISTS events(ts TEXT, kind TEXT, ref TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS chase_cases(
  case_id TEXT PRIMARY KEY, supplier_id TEXT NOT NULL, supplier_name TEXT NOT NULL,
  po_number TEXT NOT NULL, po_line INTEGER NOT NULL, material TEXT NOT NULL, issue_kind TEXT NOT NULL,
  status TEXT NOT NULL, question TEXT NOT NULL, needs_review INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_reply_summary TEXT,
  UNIQUE(supplier_id, po_number, po_line, material, issue_kind));
CREATE TABLE IF NOT EXISTS chase_events(
  id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
  detail TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chase_case_events ON chase_events(case_id, id);
CREATE TABLE IF NOT EXISTS chase_threads(
  supplier_id TEXT NOT NULL, po_number TEXT NOT NULL, initial_action_id TEXT NOT NULL,
  initial_message_id TEXT NOT NULL, attached INTEGER NOT NULL, reminder_action_id TEXT, reminder_message_id TEXT,
  PRIMARY KEY(supplier_id, po_number));
CREATE TABLE IF NOT EXISTS chase_case_messages(
  case_id TEXT NOT NULL, message_id TEXT NOT NULL, direction TEXT NOT NULL, purpose TEXT NOT NULL,
  PRIMARY KEY(case_id, message_id, direction));
CREATE INDEX IF NOT EXISTS chase_message_cases ON chase_case_messages(message_id, direction);
"""


def con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


@contextmanager
def tx():
    """One connection per unit of work: commit on success, roll back on error, and always close.
    (`with sqlite3.connect() as c` only commits; it never closes, which leaked file handles until the server ran out.)"""
    c = con()
    try:
        with c:
            yield c
    finally:
        c.close()


def init_schema(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    fields = {"provider_id", "po_numbers"}
    if fields - {r[1] for r in c.execute("PRAGMA table_info(messages)")}:
        with c:
            c.execute("BEGIN IMMEDIATE")
            for field in sorted(fields - {r[1] for r in c.execute("PRAGMA table_info(messages)")}):
                c.execute(f"ALTER TABLE messages ADD COLUMN {field} TEXT")


def log_event(kind: str, ref: str | None, text: str) -> None:
    with tx() as c:
        c.execute("INSERT INTO events VALUES (?,?,?,?)", (now().isoformat(timespec="seconds"), kind, ref, text))


def _hist(row, entry: str) -> str:
    h = json.loads(row["history"] or "[]") if row else []
    h.append({"ts": now().isoformat(timespec="seconds"), "entry": entry})
    return json.dumps(h, ensure_ascii=False)


def open_action_for(finding_id: str, kind: str | None = None) -> dict | None:
    with tx() as c:
        q = "SELECT * FROM actions WHERE finding_id=? AND status IN ('proposed','scheduled','approved','sent','escalated','auto')"
        args = [finding_id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        r = c.execute(q + " ORDER BY created_at DESC LIMIT 1", args).fetchone()
    return dict(r) if r else None


def propose(finding_id: str, title: str, rationale: str, kind: str, cost_eur: float, eur_protected: float, ce_copies: int,
            category: str | None, owner: str | None, due_in_minutes: int = 60, payload: dict | None = None,
            messages: Sequence[dict] = (), *, connection: sqlite3.Connection | None = None) -> dict:
    ap = approver_for(cost_eur, category)
    special = cfg("approval_by_kind").get(kind)
    if special and cost_eur > 0:  # Paid actions can require additional role-specific sign-off.
        people, names = cfg("people"), []
        for role in special:
            name = people.get(f"{role}:{category}") if role == "Category Lead" else people.get(role)
            if role == "Category Lead" and not name:
                role, name = "Head of Procurement", people.get("Head of Procurement")  # vacant seat -> next tier
            names.append((role, name))
        if cost_eur and approver_for(cost_eur, category).get("role") not in [r for r, _ in names]:
            names.append((ap.get("role"), ap.get("name")))
        ap = {"role": " + ".join(r for r, _ in names), "name": " + ".join(n or r for r, n in names)}
    t = now()
    status = "proposed"
    due = t + dt.timedelta(minutes=due_in_minutes)
    check = t + dt.timedelta(minutes=min(due_in_minutes, 30))
    entry = f"proposed; needs {ap.get('role')} ({ap.get('name')})" + (f" — {ap['note']}" if ap.get("note") else "")
    if policy_automatic(kind, cost_eur):
        status = "auto"
        ap = {"role": "Dockside", "name": "Dockside"}
        entry = "released by Dockside (free and reversible, no signature needed)"
    elif policy_schedules({"kind": kind, "cost_eur": cost_eur, "eur_protected": eur_protected}):
        status = "scheduled"
        due = check = t + dt.timedelta(minutes=cfg("autonomy")["act_unless_stopped"]["window_minutes"])
        entry = f"Dockside executes at {check:%H:%M} unless {ap['name']} stops it"
    aid = "ACT-" + uuid.uuid4().hex[:6].upper()
    stamp = t.isoformat(timespec="seconds")
    with (tx() if connection is None else nullcontext(connection)) as c:
        c.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            aid, finding_id, title, rationale, kind, cost_eur, eur_protected, ce_copies, category, ap.get("role"), ap.get("name"), owner,
            status, stamp, stamp, due.isoformat(timespec="seconds"), check.isoformat(timespec="seconds"), 0,
            json.dumps(payload or {}, ensure_ascii=False), _hist(None, entry)))
        for message in messages:
            add_message(c, aid, **message)
        c.execute("INSERT INTO events VALUES (?,?,?,?)", (stamp, f"action_{status}", aid, title if status == "proposed" else entry))
        if connection is not None:
            return _get(c, aid)
    return get(aid)


def add_message(c: sqlite3.Connection, action_id: str, channel: str, to_name: str, to_address: str | None,
                language: str, subject: str, body: str, body_en: str | None = None,
                po_numbers: Sequence[str] | None = None) -> str:
    mid = "MSG-" + uuid.uuid4().hex[:6].upper()
    c.execute("""INSERT INTO messages (id,action_id,channel,to_name,to_address,language,subject,body,body_en,
                                       status,created_at,po_numbers)
                 VALUES (?,?,?,?,?,?,?,?,?,'draft',?,?)""",
              (mid, action_id, channel, to_name, to_address, language, subject, body, body_en,
               now().isoformat(timespec="seconds"), json.dumps(po_numbers) if po_numbers is not None else None))
    return mid


def get(aid: str) -> dict | None:
    with tx() as c:
        return _get(c, aid)


def _get(c: sqlite3.Connection, aid: str) -> dict | None:
    r = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["payload"] = json.loads(d["payload"] or "{}")
    d["history"] = json.loads(d["history"] or "[]")
    d["messages"] = [dict(m) for m in c.execute("SELECT * FROM messages WHERE action_id=? ORDER BY created_at", (aid,))]
    return d


def list_actions(statuses: tuple = ("proposed", "scheduled", "approved", "sent", "escalated", "done", "rejected", "auto", "superseded")) -> list[dict]:
    with tx() as c:
        ids = [r[0] for r in c.execute(f"SELECT id FROM actions WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY eur_protected DESC, created_at",
                                       statuses)]
    return [get(i) for i in ids]


def decide(aid: str, decision: str, by: str, note: str = "", *, scheduled_only: bool = False,
           expires_at: str | None = None) -> dict:
    """Human decision from the web UI or Discord. approve -> messages become sendable."""
    if decision not in ("approve", "reject"):
        raise ValueError(f"Unknown decision: {decision}")
    status = "approved" if decision == "approve" else "rejected"
    with tx() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
        if not r:
            raise KeyError(aid)
        if expires_at and now() >= ts(expires_at):
            raise ValueError("This email link expired")
        allowed = ("scheduled",) if scheduled_only else ("proposed", "escalated", "scheduled")
        if r["status"] not in allowed:
            raise ValueError(f"{aid} is already {r['status']}")
        c.execute("UPDATE actions SET status=?, updated_at=?, history=? WHERE id=?",
                  (status, now().isoformat(timespec="seconds"), _hist(r, f"{status} by {by}" + (f": {note}" if note else "")), aid))
    log_event(f"action_{status}", aid, f"{by}: {note}")
    return get(aid)


def set_status(aid: str, status: str, entry: str, *, expected: str | None = None, **fields) -> dict | None:
    with tx() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
        if r is None or (expected is not None and r["status"] != expected):
            return None
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE actions SET status=?, updated_at=?, history=?{', ' + sets if sets else ''} WHERE id=?",
                  (status, now().isoformat(timespec="seconds"), _hist(r, entry), *fields.values(), aid))
    log_event(f"action_{status}", aid, entry)
    return get(aid)


def policy_automatic(kind: str, cost: float) -> bool:
    return cost == 0


def policy_schedules(a: dict) -> bool:
    """Act-unless-stopped within the Buyer limit and above the value threshold."""
    pol = cfg("autonomy")["act_unless_stopped"]
    if a["kind"] in cfg("autonomy")["always_sign"] or a["kind"] not in pol["kinds"]:
        return False
    cost = a["cost_eur"] or 0
    if not 0 < cost < pol["max_cost_eur"]:
        return False
    return (a["eur_protected"] or 0) >= pol["min_value_ratio"] * cost


def release_free() -> list[dict]:
    """Apply today's free-action policy to open decisions already in the action log."""
    with tx() as c:
        queued = [tuple(row) for row in c.execute("""
            SELECT id,status FROM actions WHERE cost_eur=0 AND status IN ('proposed','scheduled','escalated')
        """)]
    released = []
    for aid, status in queued:
        if action := set_status(aid, "auto", "Released by Dockside without a spending decision.",
                                expected=status, approver_role="Dockside", approver_name="Dockside"):
            released.append(action)
    return released


def due_scheduled() -> list[dict]:
    t = now().isoformat(timespec="seconds")
    with tx() as c:
        ids = [r[0] for r in c.execute("""SELECT a.id FROM actions a
            WHERE a.status='scheduled' AND a.next_check_at <= ? AND EXISTS (
                SELECT 1 FROM messages m WHERE m.action_id=a.id AND m.channel='approval_email' AND m.status='sent')
            """, (t,))]
    return [get(i) for i in ids]


def due_followups() -> list[dict]:
    """Proposed actions nobody decided on in time -> escalate one tier up the ladder."""
    t = now().isoformat(timespec="seconds")
    with tx() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM actions WHERE status IN ('proposed','escalated') AND next_check_at <= ?", (t,))]
    return [get(i) for i in ids]


def pending_dispatch() -> list[str]:
    with tx() as c:
        return [r[0] for r in c.execute("""SELECT a.id FROM actions a WHERE a.status IN ('approved','auto','sent')
            AND a.kind NOT IN ('chase_supplier','chase_escalation') AND (
            EXISTS (SELECT 1 FROM messages m WHERE m.action_id=a.id AND m.channel<>'approval_email' AND m.status IN ('draft','outbox')) OR
            (a.status='approved' AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.action_id=a.id
                                                 AND m.channel<>'approval_email' AND m.status IN ('sending','delivery_unknown'))))""")]


def escalate(a: dict) -> dict | None:
    t = now()
    check = (t + dt.timedelta(minutes=30)).isoformat(timespec="seconds")
    if a["kind"] in cfg("approval_by_kind"):
        return set_status(a["id"], "escalated", f"still awaiting written decision from {a['approver_name']}",
                          expected=a["status"], next_check_at=check)
    ladder = cfg("approval_ladder")
    people = cfg("people")
    roles = [x["role"] for x in ladder]
    idx = roles.index(a["approver_role"]) if a["approver_role"] in roles else 0
    nxt, name = roles[-1], roles[-1]
    for role in roles[idx + 1:]:
        person = people.get(f"{role}:{a['category']}") or people.get(role)
        if person:
            nxt, name = role, person
            break
    return set_status(a["id"], "escalated", f"no decision from {a['approver_name']} by {a['next_check_at']}; escalated to {nxt} ({name})",
                      expected=a["status"], approver_role=nxt, approver_name=name, escalation_level=a["escalation_level"] + 1,
                      next_check_at=check)
