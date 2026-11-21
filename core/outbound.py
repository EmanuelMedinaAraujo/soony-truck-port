"""Outbound channel adapter. Approved messages leave through here and nowhere else.

- email: SMTP if SMTP_HOST/SMTP_USER/SMTP_PASSWORD/MAIL_FROM are set (the teammate's registered sender address),
         otherwise the message stays in the outbox with status 'outbox' (visible in the UI).
- discord / phone_script: handed to the Discord bot via the server's listeners.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from core import actions
from core.rules import now


def _mark(mid: str, status: str) -> None:
    with actions.con() as c:
        c.execute("UPDATE messages SET status=?, sent_at=? WHERE id=?", (status, now().isoformat(timespec="seconds"), mid))


def send(m: dict) -> str:
    if m["channel"] == "email" and m.get("to_address"):
        host, user, pw, sender = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM"))
        if host and sender:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, m["to_address"], m["subject"]
            msg.set_content(m["body"] + ("\n\n---\nEN: " + m["body_en"] if m.get("body_en") else ""))
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587"))) as s:
                s.starttls()
                if user:
                    s.login(user, pw or "")
                s.send_message(msg)
            _mark(m["id"], "sent")
            actions.log_event("email_sent", m["action_id"], f"to {m['to_address']}: {m['subject']}")
            return "sent"
        _mark(m["id"], "outbox")
        actions.log_event("email_outbox", m["action_id"], f"queued for {m['to_address']} (no mailer configured): {m['subject']}")
        return "outbox"
    _mark(m["id"], "delivered")
    actions.log_event("ping", m["action_id"], f"{m['channel']} to {m['to_name']}: {m['subject']}")
    return "delivered"
