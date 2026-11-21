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
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from core import actions

REPLY_SCHEMA = """
CREATE TABLE IF NOT EXISTS replies(
  id TEXT PRIMARY KEY, message_id TEXT UNIQUE, msg_id TEXT, action_id TEXT, from_addr TEXT, from_name TEXT,
  subject TEXT, received_at TEXT, body TEXT, summary_en TEXT, handled INTEGER DEFAULT 0);
"""


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
    with actions.con() as c:
        c.executescript(REPLY_SCHEMA)
        ours = c.execute("SELECT id, action_id, subject, provider_id FROM messages WHERE channel='email' AND status='sent'").fetchall()
        known = {r[0] for r in c.execute("SELECT message_id FROM replies")}
    by_mid = {r["provider_id"]: r for r in ours if r["provider_id"]}
    by_subj = {_norm(r["subject"]): r for r in ours}
    if not ours:
        return []
    M = imaplib.IMAP4_SSL(host, int(os.environ.get("IMAP_PORT") or 993))
    M.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
    new = []
    try:
        M.select("INBOX", readonly=True)
        ids = M.search(None, "ALL")[1][0].split()
        for i in ids[-200:]:
            hdr = email.message_from_bytes(M.fetch(i, "(BODY.PEEK[HEADER])")[1][0][1])
            mid = (hdr["Message-ID"] or "").strip() or hashlib.sha1(bytes(hdr)).hexdigest()
            if mid in known:
                continue
            refs = (hdr["In-Reply-To"] or "") + " " + (hdr["References"] or "")
            ours_msg = next((m for k, m in by_mid.items() if k and k in refs), None) or by_subj.get(_norm(_h(hdr["Subject"])))
            if not ours_msg or not _h(hdr["Subject"]).lower().lstrip().startswith(("re", "aw", "odp", "回复", "답장")):
                continue
            full = email.message_from_bytes(M.fetch(i, "(BODY.PEEK[])")[1][0][1])
            name, addr = parseaddr(_h(full["From"]))
            try:
                when = parsedate_to_datetime(full["Date"]).astimezone().isoformat(timespec="seconds")
            except Exception:
                when = None
            rid = "RPL-" + hashlib.sha1(mid.encode()).hexdigest()[:6].upper()
            row = dict(id=rid, message_id=mid, msg_id=ours_msg["id"], action_id=ours_msg["action_id"], from_addr=addr, from_name=name or addr,
                       subject=_h(full["Subject"]), received_at=when, body=_strip_quote(_body(full)))
            with actions.con() as c:
                c.execute("INSERT OR IGNORE INTO replies(id,message_id,msg_id,action_id,from_addr,from_name,subject,received_at,body) VALUES (?,?,?,?,?,?,?,?,?)",
                          tuple(row.values()))
            actions.log_event("reply", row["action_id"], f"{row['from_name']} replied: {row['subject']}")
            new.append(row)
    finally:
        M.logout()
    return new


def unhandled() -> list[dict]:
    with actions.con() as c:
        c.executescript(REPLY_SCHEMA)
        return [dict(r) for r in c.execute("SELECT * FROM replies WHERE handled=0 ORDER BY received_at")]


def mark_handled(rid: str, summary_en: str) -> dict | None:
    with actions.con() as c:
        r = c.execute("SELECT * FROM replies WHERE id=?", (rid,)).fetchone()
        if not r:
            return None
        c.execute("UPDATE replies SET handled=1, summary_en=? WHERE id=?", (summary_en, rid))
    if r["action_id"]:
        a = actions.get(r["action_id"])
        if a:
            actions.set_status(a["id"], a["status"], f"reply from {r['from_name']}: {summary_en}")
    return dict(r)
