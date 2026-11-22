"""Supplier replies: read the shared mailbox (read-only, never marks mail as read) and link replies to Dockside's own messages.

Only replies to messages Dockside sent are taken (In-Reply-To / References = our Message-ID, or "Re: <our subject>").
The teammate's supplier-chaser threads in the same mailbox are left alone.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import os
import re
import uuid
from email.header import decode_header, make_header
from email.utils import parseaddr

from core import actions, rules

REPLY_SCHEMA = """
CREATE TABLE IF NOT EXISTS replies(
  id TEXT PRIMARY KEY, message_id TEXT UNIQUE, msg_id TEXT, action_id TEXT, from_addr TEXT, from_name TEXT,
  subject TEXT, received_at TEXT, body TEXT, summary_en TEXT, handled INTEGER DEFAULT 0, simulated INTEGER NOT NULL DEFAULT 0);
"""


TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS staff_tasks(
  id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES actions(id), source_reply_id TEXT NOT NULL REFERENCES replies(id),
  person TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','answered','closed')),
  created_at TEXT NOT NULL, answered_reply_id TEXT REFERENCES replies(id), answer_text TEXT, answered_at TEXT);
"""
TASK_INDEX = """CREATE UNIQUE INDEX IF NOT EXISTS active_staff_task_per_order
    ON staff_tasks(action_id) WHERE status IN ('open','answered')"""
REMINDER_SCHEMA = """CREATE TABLE IF NOT EXISTS staff_task_reminders(
    message_id TEXT PRIMARY KEY REFERENCES messages(id), task_id TEXT NOT NULL REFERENCES staff_tasks(id))"""
TRANSLATION_SCHEMA = """CREATE TABLE IF NOT EXISTS reply_translations(
    reply_id TEXT PRIMARY KEY REFERENCES replies(id), body_en TEXT NOT NULL)"""


def init_schema(c) -> None:
    c.execute(REPLY_SCHEMA)
    if "simulated" not in {r[1] for r in c.execute("PRAGMA table_info(replies)")}:
        c.execute("BEGIN IMMEDIATE")
        if "simulated" not in {r[1] for r in c.execute("PRAGMA table_info(replies)")}:
            c.execute("ALTER TABLE replies ADD COLUMN simulated INTEGER NOT NULL DEFAULT 0")
        c.commit()
    for statement in (TASK_SCHEMA, TASK_INDEX, REMINDER_SCHEMA, TRANSLATION_SCHEMA):
        c.execute(statement)


def _h(v) -> str:
    return str(make_header(decode_header(v or "")))


def _body(msg) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get("Content-Disposition", "").startswith("attachment"):
            return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            return re.sub(r"<[^>]+>", " ", html)
    return ""


def _strip_quote(text: str) -> str:
    """Keep the new part of a reply, drop the quoted original."""
    cut = re.split(r"\n(?:On .+ wrote:|W dniu .+ napisał|.+ 님이 작성:|.+写道：|-----Original Message-----|>)", text, maxsplit=1)
    return cut[0].strip()[:6000]


def _norm(subject: str) -> str:
    return re.sub(r"^(\s*(re|aw|odp|回复|답장|fw|fwd)\s*:\s*)+", "", subject or "", flags=re.I).strip().lower()


def fetch() -> list[dict]:
    """Return newly linked replies (also stored in the actions DB)."""
    host = os.environ.get("IMAP_HOST")
    if not host:
        return []
    with actions.tx() as c:
        ours = c.execute("""SELECT m.id,m.action_id,m.subject,m.provider_id,m.to_address,a.kind
            FROM messages m LEFT JOIN actions a ON a.id=m.action_id WHERE m.channel='email'
            AND (a.kind IS NULL OR a.kind<>'chase_escalation')
            AND (m.status='sent' OR (a.kind='chase_supplier' AND m.status IN ('sending','delivery_unknown')))""").fetchall()
        known = {r[0] for r in c.execute("SELECT message_id FROM replies")}
    by_mid = {r["provider_id"]: r for r in ours if r["provider_id"]}
    by_subj = {}
    for r in ours:
        if not r["provider_id"] and r["kind"] != "chase_supplier":
            by_subj.setdefault((_norm(r["subject"]), (r["to_address"] or "").casefold()), []).append(r)
    if not ours:
        return []
    M = imaplib.IMAP4_SSL(host, int(os.environ.get("IMAP_PORT") or 993), timeout=20)
    M.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
    new = []
    try:
        selected = M.select("INBOX", readonly=True)
        status, matches = M.search(None, "ALL")
        if (selected and selected[0] != "OK") or status != "OK":
            raise imaplib.IMAP4.error("Supplier mailbox could not be read")
        ids = matches[0].split()
        for i in ids[-200:]:
            status, header = M.fetch(i, "(BODY.PEEK[HEADER])")
            if status != "OK":
                raise imaplib.IMAP4.error("Supplier mailbox header could not be read")
            hdr = email.message_from_bytes(header[0][1])
            mid = (hdr["Message-ID"] or "").strip() or hashlib.sha1(bytes(hdr)).hexdigest()
            if mid in known:
                continue
            subject = _h(hdr["Subject"])
            if "[sc:" in subject.casefold():
                continue
            name, addr = parseaddr(_h(hdr["From"]))
            direct = re.findall(r"<[^<>]+>", hdr["In-Reply-To"] or "")
            refs = re.findall(r"<[^<>]+>", hdr["References"] or "")
            ours_msg = next((by_mid[ref] for ref in direct + refs[::-1] if ref in by_mid), None)
            if ours_msg is None:
                candidates = by_subj.get((_norm(subject), addr.casefold()), [])
                if len(candidates) == 1 and _norm(subject) != subject.strip().lower():
                    ours_msg = candidates[0]
            if not ours_msg or (ours_msg["to_address"] or "").casefold() != addr.casefold():
                continue
            status, content = M.fetch(i, "(BODY.PEEK[])")
            if status != "OK":
                raise imaplib.IMAP4.error("Supplier reply could not be read")
            full = email.message_from_bytes(content[0][1])
            # The sender's Date header can precede delivery. Use when Dockside observed the reply.
            when = actions.now().isoformat(timespec="seconds")
            rid = "RPL-" + hashlib.sha1(mid.encode()).hexdigest()[:6].upper()
            row = dict(id=rid, message_id=mid, msg_id=ours_msg["id"], action_id=ours_msg["action_id"], from_addr=addr, from_name=name or addr,
                       subject=_h(full["Subject"]), received_at=when, body=_strip_quote(_body(full)))
            with actions.tx() as c:
                inserted = c.execute("INSERT OR IGNORE INTO replies(id,message_id,msg_id,action_id,from_addr,from_name,subject,received_at,body) VALUES (?,?,?,?,?,?,?,?,?)",
                                     tuple(row.values())).rowcount
            if not inserted:
                continue
            actions.log_event("reply", row["action_id"], f"{row['from_name']} replied: {row['subject']}")
            new.append(row)
    finally:
        M.logout()
    return new


def unhandled() -> list[dict]:
    if not actions.DB.is_file():
        return []
    with actions.read() as c:
        return [dict(r) for r in c.execute("""SELECT r.* FROM replies r LEFT JOIN actions a ON a.id=r.action_id
            WHERE r.handled=0 AND (a.kind IS NULL OR a.kind<>'chase_supplier')
            AND instr(lower(r.subject),'[sc:')=0
            AND NOT (? AND EXISTS (SELECT 1 FROM chase_case_messages m JOIN chase_cases cc USING(case_id)
                WHERE m.message_id=r.id AND m.direction='inbound' AND m.purpose='received' AND cc.status='answered'))
            ORDER BY r.received_at""", (rules.cfg("chase")["enabled"],))]


def _staff_reply(c, action_id, msg_id, person, text, subject, channel, *, task_id=None):
    rid = "RPL-" + uuid.uuid4().hex[:6].upper()
    stamp = rules.now().isoformat(timespec="seconds")
    message_id = f"{task_id}:{rid}" if task_id else f"{channel}:{rid}"
    c.execute("""INSERT INTO replies(id,message_id,msg_id,action_id,from_name,subject,received_at,body)
                 VALUES (?,?,?,?,?,?,?,?)""", (rid, message_id, msg_id, action_id, person, subject, stamp, text))
    c.execute("INSERT INTO events VALUES (?,?,?,?)",
              (stamp, "reply", action_id, f"{person} answered {task_id or msg_id} via {channel}"))
    return dict(c.execute("SELECT * FROM replies WHERE id=?", (rid,)).fetchone())


def add_staff_answer(c, question: dict, person: str, text: str, channel: str) -> dict:
    message = c.execute("SELECT id, action_id, subject FROM messages WHERE id=?", (question["id"],)).fetchone()
    return _staff_reply(c, message["action_id"], message["id"], person, text,
                        f"Staff answer: {message['subject'] or ''}".strip(), channel)


def add_task_answer(c, task: dict, person: str, text: str, channel: str) -> dict:
    """Persist the answer and enqueue its reply atomically with the task transition."""
    reply = _staff_reply(c, task["action_id"], None, person, text, f"Answer to {task['id']}", channel, task_id=task["id"])
    c.execute("""UPDATE staff_tasks SET status='answered', answered_reply_id=?, answer_text=?, answered_at=? WHERE id=?""",
              (reply["id"], text, reply["received_at"], task["id"]))
    return reply


def get_reply(rid: str) -> dict | None:
    if not actions.DB.is_file():
        return None
    with actions.read() as c:
        row = c.execute("SELECT * FROM replies WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else None


def mark_handled(rid: str, summary_en: str) -> dict | None:
    with actions.tx() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM replies WHERE id=?", (rid,)).fetchone()
        if not r:
            return None
        if r["simulated"] and not summary_en.startswith("Simulated call:"):
            summary_en = f"Simulated call: {summary_en}"
        if not r["handled"]:
            c.execute("UPDATE replies SET handled=1, summary_en=? WHERE id=?", (summary_en, rid))
            a = c.execute("SELECT history FROM actions WHERE id=?", (r["action_id"],)).fetchone()
            if a:
                c.execute("UPDATE actions SET history=? WHERE id=?",
                          (actions._hist(a, f"reply from {r['from_name']}: {summary_en}"), r["action_id"]))
    return dict(r)
