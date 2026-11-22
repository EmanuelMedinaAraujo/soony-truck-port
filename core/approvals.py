"""Email the spending decision and keep the action's link credentials out of messages."""
from __future__ import annotations

import datetime as dt
import os
import re
import secrets
from email.utils import parseaddr
from urllib.parse import urlsplit

from core import actions, outbound, rules

WAITING = ("scheduled", "proposed", "escalated")
PATH = "/approval/"
SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_links(
    action_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL, approver_name TEXT NOT NULL
)
"""


def _address(name: str) -> str | None:
    if configured := rules.cfg("staff_email").get(name):
        return configured
    _, sender = parseaddr(os.environ.get("MAIL_FROM", ""))
    local, _, domain = sender.rpartition("@")
    tag = ".".join(re.findall(r"[a-z0-9]+", name.casefold()))
    if local and tag and domain.casefold() in {"gmail.com", "googlemail.com"}:
        return f"{local.split('+', 1)[0]}+{tag}@{domain}"
    return None


def _body(a, due: dt.datetime | None) -> str:
    timing = (f"Dockside goes ahead at {due:%d %b %Y, %H:%M %z} unless you stop it."
              if due else "Dockside waits for explicit approval before releasing this action.")
    return (f"Hi {a['approver_name']},\n\n"
            f"What: {a['title']}\nWhy: {a['rationale']}\n"
            f"Cost: €{a['cost_eur']:,.2f}\n"
            f"Protects: €{a['eur_protected'] or 0:,.2f} and {a['ce_copies'] or 0:,} {rules.cfg('edition_labels')['priority_copies']}.\n\n"
            f"{timing}\n")


def _queue(aid: str) -> tuple[dict, str] | None:
    with actions.tx() as c:
        c.execute(SCHEMA)
        c.execute("BEGIN IMMEDIATE")
        a = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
        if not a or a["status"] not in WAITING or not (a["cost_eur"] or 0) > 0:
            return None
        link = c.execute("SELECT * FROM approval_links WHERE action_id=?", (aid,)).fetchone()
        if link:
            m = c.execute("SELECT * FROM messages WHERE id=?", (link["message_id"],)).fetchone()
            if m["status"] not in {"draft", "outbox"}:
                return None
        recipients = [_address(name.strip()) for name in a["approver_name"].split(" + ")] if a["approver_name"] else []
        if not recipients or not all(recipients):
            return None
        address = ", ".join(dict.fromkeys(recipients))
        now = rules.now()
        due = None
        if a["status"] == "scheduled":
            due = now + dt.timedelta(minutes=rules.cfg("autonomy")["act_unless_stopped"]["window_minutes"])
            stamp = due.isoformat(timespec="seconds")
            c.execute("UPDATE actions SET due_at=?,next_check_at=? WHERE id=?", (stamp, stamp, aid))
        expires = due or now + dt.timedelta(days=7)
        body = _body(a, due)
        subject = f"Dockside approval: {a['title']}"
        if link:
            token, mid = link["token"], link["message_id"]
            c.execute("UPDATE messages SET body=?,to_name=?,to_address=? WHERE id=?",
                      (body, a["approver_name"], address, mid))
            c.execute("UPDATE approval_links SET expires_at=?,approver_name=? WHERE action_id=?",
                      (expires.isoformat(timespec="seconds"), a["approver_name"], aid))
        else:
            token = secrets.token_urlsafe(32)
            mid = actions.add_message(c, aid, "approval_email", a["approver_name"], address, "en", subject, body)
            c.execute("INSERT INTO approval_links VALUES (?,?,?,?,?)",
                      (aid, mid, token, expires.isoformat(timespec="seconds"), a["approver_name"]))
        return dict(c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()), token


def email(aid: str) -> None:
    base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    url = urlsplit(base)
    if url.scheme not in {"https", "http"} or not url.netloc or url.query or url.fragment:
        return
    with outbound.DISPATCH_LOCK:
        queued = _queue(aid)
        if not queued:
            return
        message, token = queued
        link = f"{base}{PATH}{token}"
        message["body"] += f"\nApprove now: {link}/approve\nStop: {link}/reject\n"
        try:
            outbound.send(message)
        except (OSError, ValueError) as exc:
            outbound._mark(message["id"], "outbox", expected=("draft", "outbox"))
            actions.log_event("approval_email_error", aid, type(exc).__name__)


def email_open() -> None:
    with actions.tx() as c:
        ids = [row[0] for row in c.execute("""SELECT id FROM actions WHERE cost_eur>0
                                              AND status IN ('scheduled','proposed','escalated')""")]
    for aid in ids:
        email(aid)


def lookup(token: str) -> tuple[dict, str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise KeyError("Unknown approval link")
    with actions.tx() as c:
        c.execute(SCHEMA)
        row = c.execute("""SELECT l.*, a.title, a.cost_eur, a.status FROM approval_links l
                           JOIN actions a ON a.id=l.action_id
                           WHERE l.token=?""", (token,)).fetchone()
    if not row:
        raise KeyError("Unknown approval link")
    if row["status"] not in WAITING:
        return dict(row), "used"
    if rules.now() >= rules.ts(row["expires_at"]):
        return dict(row), "expired"
    return dict(row), "open"
