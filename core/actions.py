"""Action log: everything the agents propose, who must approve it, what was sent, and when to follow up.

Lives in data/actions.db, which is NOT rebuilt (unlike the master DB): it is the system's memory.
"""
from __future__ import annotations

import datetime as dt
import json
import re
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


COMMITMENTS = ("order", "dispatch", "book_truck", "expedite")
PART = re.compile(r"\b[A-Z]{2,4}-[A-Z0-9]{2,5}-\d{2,4}\b")


def open_commitment(kind: str, finding_id: str, recipients: Sequence[str], hours: int = 72) -> dict | None:
    """A paid commitment of the same kind for the same part and recipient that is still standing.
    Two order emails for the same 8,000 books would be two orders: the second must replace the first, not add to it."""
    if kind not in COMMITMENTS:
        return None
    parts = set(PART.findall(finding_id))
    wanted = {r.casefold() for r in recipients if r}
    since = (now() - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
    with tx() as c:
        rows = c.execute("""SELECT * FROM actions WHERE kind=? AND cost_eur>0 AND created_at>=?
                            AND status IN ('proposed','scheduled','approved','sent','escalated')""", (kind, since)).fetchall()
        for row in rows:
            if parts and not parts & set(PART.findall(row["finding_id"] or "")):
                continue
            sent_to = {(m[0] or "").casefold() for m in c.execute("SELECT to_address FROM messages WHERE action_id=?", (row["id"],))} | {
                (m[0] or "").casefold() for m in c.execute("SELECT to_name FROM messages WHERE action_id=?", (row["id"],))}
            if not wanted or wanted & sent_to:
                return dict(row)
    return None


def commitment_for(finding_id: str) -> dict | None:
    """Find the paid action that a named finding's reminder should follow."""
    with tx() as c:
        rows = c.execute("""SELECT id FROM actions WHERE finding_id=? AND cost_eur>0
                            AND status IN ('proposed','scheduled','approved','sent','done','escalated')""",
                         (finding_id,)).fetchall()
        return _get(c, rows[0]["id"]) if len(rows) == 1 else None


class UnverifiedApprover(PermissionError):
    pass


def signers(action) -> tuple[str, ...]:
    return tuple(dict.fromkeys(name.strip() for name in (action["approver_name"] or "").split(" + ") if name.strip()))


def hold(aid: str, reason: str | None, *, supersede: bool = False, expire: bool = False) -> dict:
    """Keep the current queue and record changes to the release reason."""
    with tx() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
        payload = json.loads(row["payload"] or "{}")
        status = "superseded" if supersede else "expired" if expire else row["status"]
        if reason == payload.get("held_reason") and status == row["status"]:
            return _get(c, aid)
        stamp = now().isoformat(timespec="seconds")
        history = row["history"]
        if reason:
            payload["held_reason"] = reason
            history = _hist(row, f"Held before release: {reason}")
            c.execute("INSERT INTO events VALUES (?,?,?,?)", (stamp, "release_held", aid, reason))
        else:
            payload.pop("held_reason", None)
        c.execute("UPDATE actions SET status=?,payload=?,history=?,updated_at=? WHERE id=?",
                  (status, json.dumps(payload, ensure_ascii=False), history, stamp, aid))
        return _get(c, aid)


def propose(finding_id: str, title: str, rationale: str, kind: str, cost_eur: float, eur_protected: float, ce_copies: int,
            category: str | None, owner: str | None, due_in_minutes: int = 60, payload: dict | None = None,
            messages: Sequence[dict] = (), *, connection: sqlite3.Connection | None = None,
            parent: dict | None = None, repeat_commitment: bool = False) -> dict:
    from core import release

    payload = dict(payload or {})
    for key in ("signatures", "commitment_digest", "repeat_commitment", "held_reason"):
        payload.pop(key, None)
    aid = "ACT-" + uuid.uuid4().hex[:6].upper()
    intent = release.instruction_kind(kind, messages)
    typed = bool(messages and (payload.get("commitment") or cost_eur > 0 or repeat_commitment))
    proposal = {"id": aid, "finding_id": finding_id, "kind": kind, "cost_eur": cost_eur, "payload": payload,
                "messages": messages}
    if repeat_commitment:
        payload["repeat_commitment"] = parent["id"] if parent else "missing"
        inherited = release.terms_for(parent) if parent else None
        if cost_eur == 0 and not payload.get("commitment") and inherited:
            payload["commitment"] = inherited
        else:
            payload.pop("commitment", None)
    elif typed:
        payload["commitment"] = release.describe(proposal, parent=parent)
    if terms := release.terms_for(proposal):
        try:
            payload["commitment_digest"] = release.fingerprint(terms)
        except ValueError:
            pass  # Malformed terms are held before approval.
    if not typed and (reference := release.delivery_reference(payload.get("delivery_id"))):
        messages = [dict(m, body=f"{m['body']}\n\n{reference}") if m["channel"] == "email" else m for m in messages]
    special = cfg("approval_by_kind").get((terms or {}).get("instruction") or intent or kind)
    # An unverified FX estimate cannot reduce the required approval tier.
    routing_cost = cost_eur
    if terms and isinstance(terms["total_price"], (float, int)) and terms["total_price"] > 0:
        routing_cost = terms["total_price"] if terms["currency"] == "EUR" else float("inf")
    ap = approver_for(routing_cost, category)
    if (cost_eur > 0 or typed) and (special or " + " in ap.get("role", "")):
        people, names = cfg("people"), []
        for role in dict.fromkeys([*(special or []), *ap["role"].split(" + ")]):
            name = people.get(f"{role}:{category}") or people.get(role)
            if role == "Category Lead" and not name:
                role, name = "Head of Procurement", people.get("Head of Procurement")  # vacant seat -> next tier
            names.append((role, name))
        ap = {"role": " + ".join(r for r, _ in names), "name": " + ".join(n or r for r, n in names)}
    t = now()
    status = "proposed"
    due = t + dt.timedelta(minutes=due_in_minutes)
    check = t + dt.timedelta(minutes=min(due_in_minutes, 30))
    entry = f"proposed; needs {ap.get('role')} ({ap.get('name')})" + (f" — {ap['note']}" if ap.get("note") else "")
    if policy_automatic(kind, cost_eur) and (not typed or repeat_commitment):
        status = "auto"
        ap = {"role": "Dockside", "name": "Dockside"}
        entry = "released by Dockside (free and reversible, no signature needed)"
    elif not typed and policy_schedules({"kind": kind, "cost_eur": cost_eur, "eur_protected": eur_protected, "approver_name": ap.get("name")}):
        status = "scheduled"
        due = check = t + dt.timedelta(minutes=cfg("autonomy")["act_unless_stopped"]["window_minutes"])
        entry = f"Dockside executes at {check:%H:%M} unless {ap['name']} stops it"
    stamp = t.isoformat(timespec="seconds")
    if release.applies(proposal) and (reason := release.proposal_blocker(proposal)):
        payload["held_reason"] = reason
        entry = f"Held before release: {reason}"
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
    d["held_reason"] = d["payload"].get("held_reason")
    d["signatures"] = d["payload"].get("signatures", {})
    terms = d["payload"].get("commitment") or {}
    d["commitment_id"] = terms.get("commitment_id") or terms.get("key") or d["payload"].get("commitment_key")
    d["messages"] = [dict(m) for m in c.execute("SELECT * FROM messages WHERE action_id=? ORDER BY created_at", (aid,))]
    return d


def list_actions(statuses: tuple = ("proposed", "scheduled", "approved", "sent", "escalated", "done", "rejected", "auto", "superseded", "expired")) -> list[dict]:
    with tx() as c:
        ids = [r[0] for r in c.execute(f"SELECT id FROM actions WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY eur_protected DESC, created_at",
                                       statuses)]
    return [get(i) for i in ids]


def decide(aid: str, decision: str, by: str, note: str = "", *, source: str | None = None,
           expires_at: str | None = None, commitment_digest: str | None = None) -> dict:
    """Record a verified decision and wait for all required approvals."""
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
        if r["status"] not in ("proposed", "escalated", "scheduled"):
            raise ValueError(f"{aid} is already {r['status']}")
        payload = json.loads(r["payload"] or "{}")
        stamp = now().isoformat(timespec="seconds")
        actor = f"{by} ({source})" if source else by
        event = f"action_{status}"
        entry = f"{status} by {actor}"
        from core import release
        action = _get(c, aid)
        gated = release.applies(action)
        if r["cost_eur"] > 0 or gated:
            required = set(signers(r))
            if source not in {"dashboard", "Discord", "email", "local demo"} or by not in required:
                raise UnverifiedApprover(f"{aid} requires a verified decision from {r['approver_name'] or r['approver_role']}.")
            if decision == "approve":
                digest = release.approval_digest(action)
                if gated and (reason := release.proposal_blocker(action)):
                    raise ValueError(reason)
                if gated and digest != commitment_digest:
                    raise ValueError("Review the current shipment instruction before signing")
                signatures = payload.setdefault("signatures", {})
                if by in signatures:
                    raise ValueError(f"{by} already signed")
                signatures[by] = {"source": source, "signed_at": stamp,
                                  **({"commitment_digest": digest} if digest else {})}
                if required - signatures.keys():
                    status, event = r["status"], "approval_signed"
                    entry = f"Signed by {actor}; awaiting {', '.join(sorted(required - signatures.keys()))}"
        if note:
            entry += f": {note}"
        c.execute("UPDATE actions SET status=?, updated_at=?, history=?, payload=? WHERE id=?",
                  (status, stamp, _hist(r, entry), json.dumps(payload, ensure_ascii=False), aid))
        c.execute("INSERT INTO events VALUES (?,?,?,?)", (stamp, event, aid, entry))
        return _get(c, aid)


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
    if "approver_name" in a and len(signers(a)) != 1:
        return False
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
    from core import release
    for aid, status in queued:
        if release.applies(get(aid)):
            continue
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
