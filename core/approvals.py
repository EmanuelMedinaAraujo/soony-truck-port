"""Email the spending decision and keep the action's link credentials out of messages."""
from __future__ import annotations

import datetime as dt
import os
import re
import secrets
from email.utils import parseaddr
from urllib.parse import urlsplit

from core import actions, outbound, release, rules

WAITING = ("scheduled", "proposed", "escalated")
PATH = "/approval/"
SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_signer_links(
    action_id TEXT NOT NULL, message_id TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL, approver_name TEXT NOT NULL,
    PRIMARY KEY(action_id, approver_name)
)
"""


def _address(name: str, *, single: bool) -> str | None:
    if configured := rules.cfg("staff_email").get(name):
        _, address = parseaddr(configured)
        return address if address == configured and "@" in address else None
    if not single or not release.setting("demo_approval_aliases", False):
        return None
    _, sender = parseaddr(os.environ.get("MAIL_FROM", ""))
    local, _, domain = sender.rpartition("@")
    tag = ".".join(re.findall(r"[a-z0-9]+", name.casefold()))
    if local and tag and domain.casefold() in {"gmail.com", "googlemail.com"}:
        return f"{local.split('+', 1)[0]}+{tag}@{domain}"
    return None


def _body(a, name: str, due: dt.datetime | None) -> str:
    timing = (f"Dockside goes ahead at {due:%d %b %Y, %H:%M %z} unless you stop it."
              if due else "Dockside waits for explicit approval before releasing this action.")
    terms = release.terms_for(a)
    instruction = "\n" + release.render_instruction(terms)[1] + "\n" if terms else ""
    return (f"Hi {name},\n\n"
            f"What: {a['title']}\nWhy: {a['rationale']}\n"
            f"Cost: €{a['cost_eur']:,.2f}\n"
            f"Protects: €{a['eur_protected'] or 0:,.2f} and {a['ce_copies'] or 0:,} {rules.cfg('edition_labels')['priority_copies']}.\n\n"
            f"{timing}\n{instruction}")


def _mailbox(address: str) -> str:
    local, _, host = address.casefold().partition("@")
    if host in {"gmail.com", "googlemail.com"}:
        return local.split("+", 1)[0].replace(".", "") + "@gmail.com"
    return address.casefold()


def _recipients(action) -> dict[str, str]:
    names = actions.signers(action)
    addresses = {name: _address(name, single=len(names) == 1) for name in names}
    return {name: address for name, address in addresses.items() if address and
            sum(_mailbox(other) == _mailbox(address) for other in addresses.values() if other) == 1}


def notified(c, aid: str, name: str) -> bool:
    c.execute(SCHEMA)
    row = c.execute("""SELECT m.to_address, m.status FROM approval_signer_links l
                       JOIN messages m ON m.id=l.message_id WHERE l.action_id=? AND l.approver_name=?""",
                    (aid, name)).fetchone()
    address = _address(name, single=True)
    return bool(row and address and row["status"] == "sent" and row["to_address"].casefold() == address.casefold())


def _queue(aid: str) -> list[tuple[dict, str]]:
    with actions.tx() as c:
        c.execute(SCHEMA)
        c.execute("BEGIN IMMEDIATE")
        a = actions._get(c, aid)
        if not a or not (a["cost_eur"] > 0 or release.applies(a)) or not release.decision_state(a)["can_approve"]:
            return []
        result = []
        now = rules.now()
        for name, address in _recipients(a).items():
            if name in a["signatures"]:
                continue
            link = c.execute("SELECT * FROM approval_signer_links WHERE action_id=? AND approver_name=?",
                             (aid, name)).fetchone()
            if link:
                m = c.execute("SELECT * FROM messages WHERE id=?", (link["message_id"],)).fetchone()
                same_address = address.casefold() == m["to_address"].casefold()
                terms = release.terms_for(a)
                same_terms = not terms or m["body"].endswith(release.terms_block(terms) + "\n")
                if same_address and same_terms and (m["status"] in outbound.UNCONFIRMED or (
                        m["status"] == "sent" and (a["status"] == "scheduled" or rules.now() < rules.ts(link["expires_at"])))):
                    continue
            due = None
            if a["status"] == "scheduled" and actions.policy_schedules(a):
                due = now + dt.timedelta(minutes=rules.cfg("autonomy")["act_unless_stopped"]["window_minutes"])
                stamp = due.isoformat(timespec="seconds")
                c.execute("UPDATE actions SET due_at=?,next_check_at=? WHERE id=?", (stamp, stamp, aid))
            expires = due or now + dt.timedelta(days=7)
            body = _body(a, name, due)
            subject = f"Dockside approval: {a['title']}"
            token = secrets.token_urlsafe(32)
            mid = (link["message_id"] if link and m["status"] in {"draft", "outbox"} else
                   actions.add_message(c, aid, "approval_email", name, address, "en", subject, body))
            c.execute("UPDATE messages SET body=?,to_address=? WHERE id=?", (body, address, mid))
            c.execute("INSERT OR REPLACE INTO approval_signer_links VALUES (?,?,?,?,?)",
                      (aid, mid, token, expires.isoformat(timespec="seconds"), name))
            result.append((dict(c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()), token))
        return result


def email(aid: str) -> None:
    base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    url = urlsplit(base)
    if url.scheme not in {"https", "http"} or not url.netloc or url.query or url.fragment:
        return
    with outbound.DISPATCH_LOCK:
        for message, token in _queue(aid):
            link = f"{base}{PATH}{token}"
            message["body"] += f"\nApprove now: {link}/approve\nStop: {link}/reject\n"
            try:
                outbound.send(message)
            except (OSError, ValueError) as exc:
                outbound._mark(message["id"], "outbox", expected=("draft", "outbox"))
                actions.log_event("approval_email_error", aid, type(exc).__name__)


def email_open() -> None:
    with actions.tx() as c:
        ids = [row[0] for row in c.execute("""SELECT id FROM actions
                                              WHERE status IN ('scheduled','proposed','escalated')""")]
    for aid in ids:
        email(aid)


def lookup(token: str) -> tuple[dict, str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise KeyError("Unknown approval link")
    with actions.tx() as c:
        c.execute(SCHEMA)
        row = c.execute("""SELECT l.*, a.title, a.cost_eur, a.status, m.to_address, m.status AS message_status,
                                  m.body AS approval_body
                           FROM approval_signer_links l JOIN actions a ON a.id=l.action_id
                           JOIN messages m ON m.id=l.message_id
                           WHERE l.token=?""", (token,)).fetchone()
        action = actions._get(c, row["action_id"]) if row else None
    if not row:
        raise KeyError("Unknown approval link")
    address = _recipients(action).get(row["approver_name"])
    if not address or row["message_status"] != "sent" or address.casefold() != row["to_address"].casefold():
        raise KeyError("Unknown approval link")
    if row["status"] not in WAITING or row["approver_name"] in action["signatures"]:
        return dict(row), "used"
    if rules.now() >= rules.ts(row["expires_at"]) or release.decision_state(action)["terminal"]:
        return dict(row), "expired"
    terms = release.terms_for(action)
    if release.applies(action) and (not terms or release.proposal_blocker(action) or
                                    not row["approval_body"].endswith(release.terms_block(terms) + "\n")):
        return dict(row), "used"
    return {**dict(row), "commitment_digest": release.approval_digest(action), "terms": terms}, "open"
