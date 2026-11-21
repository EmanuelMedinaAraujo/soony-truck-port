"""Outbound channel adapter. Approved messages leave through here and nowhere else.

- email: SMTP if SMTP_HOST/SMTP_USER/SMTP_PASSWORD/MAIL_FROM are set (the teammate's registered sender address),
         otherwise the message stays in the outbox with status 'outbox' (visible in the UI).
- discord / phone_script: handed to the Discord bot via the server's listeners.
"""
from __future__ import annotations

import os
import smtplib
import threading
from email.message import EmailMessage

from core import actions
from core.rules import now

DISPATCH_LOCK = threading.Lock()
UNCONFIRMED = {"sending", "delivery_unknown"}


def _mark(mid: str, status: str, provider_id: str | None = None) -> None:
    with actions.con() as c:
        sent_at = now().isoformat(timespec="seconds") if status in ("sent", "delivered") else None
        c.execute("UPDATE messages SET status=?, sent_at=?, provider_id=COALESCE(?,provider_id) WHERE id=?",
                  (status, sent_at, provider_id, mid))


def send(m: dict, in_reply_to: str | None = None) -> str:
    if m["channel"] == "email":
        if not m.get("to_address"):
            raise ValueError("E-mail draft has no recipient")
        host, user, pw, sender = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM"))
        if host and sender:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, m["to_address"], m["subject"]
            msg["Message-ID"] = f"<{m['id']}@{sender.rsplit('@', 1)[-1]}>"
            if in_reply_to:
                msg["In-Reply-To"] = in_reply_to
                msg["References"] = in_reply_to
            msg.set_content(m["body"] + ("\n\n---\nEN: " + m["body_en"] if m.get("body_en") else ""))
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=20) as s:
                s.starttls()
                if user:
                    s.login(user, pw or "")
                _mark(m["id"], "sending", msg["Message-ID"])
                try:
                    s.send_message(msg)
                except smtplib.SMTPResponseException:
                    raise  # the server explicitly declined this message
                except OSError:
                    _mark(m["id"], "delivery_unknown")
                    actions.log_event("dispatch_error", m["action_id"], f"{m['subject']}: SMTP acceptance is unknown")
                    return "delivery_unknown"
                _mark(m["id"], "sent", msg["Message-ID"])
            actions.log_event("email_sent", m["action_id"], f"to {m['to_address']}: {m['subject']}")
            return "sent"
        _mark(m["id"], "outbox")
        if m["status"] != "outbox":
            actions.log_event("email_outbox", m["action_id"], f"queued for {m['to_address']} (no mailer configured): {m['subject']}")
        return "outbox"
    _mark(m["id"], "delivered")
    actions.log_event("ping", m["action_id"], f"{m['channel']} to {m['to_name']}: {m['subject']}")
    return "delivered"


def dispatch(action_id: str, entry: str = "messages released after approval") -> dict:
    """Release only pending messages for an action that is already authorized."""
    with DISPATCH_LOCK:
        action = actions.get(action_id)
        if action["status"] not in ("approved", "auto", "sent"):
            return action
        reply = None
        if action["finding_id"].startswith("reply-"):
            from core import inbox
            reply = inbox.get_reply(action["finding_id"].removeprefix("reply-"))
        pending = any(m["status"] in UNCONFIRMED for m in action["messages"])
        for m in action["messages"]:
            if m["status"] not in ("draft", "outbox"):
                continue
            parent = None
            if reply and (m["to_address"] or "").casefold() == reply["from_addr"].casefold():
                ref = reply["message_id"]
                if ref.startswith("<") and ref.endswith(">"):
                    parent = ref
            try:
                pending |= send(m, parent) in {"outbox", "delivery_unknown"}
            except (OSError, ValueError) as exc:
                pending = True
                current = next(row for row in actions.get(action_id)["messages"] if row["id"] == m["id"])
                if current["status"] != "sent":
                    _mark(m["id"], "outbox")
                    actions.log_event("dispatch_error", action_id, f"{m['subject']}: {type(exc).__name__}")
        if action["status"] == "approved" and not pending:
            return actions.set_status(action_id, "sent", entry)
        return actions.get(action_id)
