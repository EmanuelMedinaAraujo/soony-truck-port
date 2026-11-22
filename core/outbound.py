"""Outbound channel adapter. Approved messages leave through here and nowhere else.

- email: SMTP if SMTP_HOST/SMTP_USER/SMTP_PASSWORD/MAIL_FROM are set (the teammate's registered sender address),
         otherwise the message stays in the outbox with status 'outbox' (visible in the UI).
- discord / phone_script: handed to the Discord bot via the server's listeners.
"""
from __future__ import annotations

import json
import os
import smtplib
import threading
from email.message import EmailMessage

from core import actions, release
from core.rules import cfg, now, shipment_commitment_blocker

DISPATCH_LOCK = threading.RLock()
UNCONFIRMED = {"sending", "delivery_unknown"}


def _mark(mid: str, status: str, provider_id: str | None = None, *, expected: tuple[str, ...] = (),
          approval: dict | None = None) -> int:
    with actions.tx() as c:
        sent_at = now().isoformat(timespec="seconds") if status in ("sent", "delivered") else None
        condition = " AND status IN (" + ",".join("?" for _ in expected) + ")" if expected else ""
        changed = c.execute("UPDATE messages SET status=?, sent_at=?, provider_id=COALESCE(?,provider_id) WHERE id=?" + condition,
                         (status, sent_at, provider_id, mid, *expected)).rowcount
        if changed and approval:
            aid = approval["action_id"]
            a = c.execute("SELECT history FROM actions WHERE id=?", (aid,)).fetchone()
            line = f"Emailed {approval['to_name']} for approval"
            c.execute("UPDATE actions SET history=? WHERE id=?", (actions._hist(a, line), aid))
            c.execute("INSERT INTO events VALUES (?,?,?,?)", (sent_at, "approval_email_sent", aid, line))
        return changed


class ReleaseHeld(Exception):
    pass


def _verify_release(action_id: str) -> None:
    action = actions.get(action_id)
    ok, reason = release.check(action)
    if not ok:
        raise ReleaseHeld(reason)
    if action["held_reason"]:
        actions.hold(action_id, None)


def send(m: dict, in_reply_to: str | None = None) -> str:
    approval = m["channel"] == "approval_email"
    with actions.tx() as c:
        row = c.execute("SELECT cost_eur, payload FROM actions WHERE id=?", (m["action_id"],)).fetchone()
    paid = not approval and row and row["cost_eur"] > 0
    payload = json.loads(row["payload"] or "{}") if row else {}
    commitment_id = payload.get("commitment", {}).get("key") or payload.get("commitment_key")
    if paid and m["channel"] != "email":
        raise ReleaseHeld("paid release requires email delivery")
    if m["channel"] == "email" or approval:
        if not m.get("to_address"):
            raise ValueError("E-mail draft has no recipient")
        host, user, pw, sender = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM"))
        if host and sender:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, m["to_address"], m["subject"]
            msg["Message-ID"] = f"<{m['id']}@{sender.rsplit('@', 1)[-1]}>"
            if commitment_id:
                msg["X-Dockside-Commitment"] = commitment_id
            if in_reply_to:
                msg["In-Reply-To"] = in_reply_to
                msg["References"] = in_reply_to
            msg.set_content(m["body"] + ("\n\n---\nEN: " + m["body_en"] if m.get("body_en") else ""))
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=20) as s:
                s.starttls()
                if user:
                    s.login(user, pw or "")
                if paid:
                    _verify_release(m["action_id"])
                if not _mark(m["id"], "sending", msg["Message-ID"], expected=("draft", "outbox")):
                    return "sending"
                try:
                    s.send_message(msg)
                except smtplib.SMTPResponseException:
                    _mark(m["id"], "outbox", expected=("sending",))
                    raise  # the server explicitly declined this message
                except OSError:
                    _mark(m["id"], "delivery_unknown")
                    actions.log_event("dispatch_error", m["action_id"], f"{m['subject']}: SMTP acceptance is unknown")
                    return "delivery_unknown"
                _mark(m["id"], "sent", msg["Message-ID"], approval=m if approval else None)
            if not approval:
                actions.log_event("email_sent", m["action_id"], f"to {m['to_address']}: {m['subject']}")
            return "sent"
        if paid:
            _verify_release(m["action_id"])
        _mark(m["id"], "outbox", expected=("draft", "outbox"))
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
        if action["status"] not in ("approved", "auto", "sent", "scheduled"):
            return action
        if action["kind"] in {"chase_supplier", "chase_escalation"} and not cfg("chase")["enabled"]:
            return action
        order_refs = (po for message in action["messages"] for po in json.loads(message.get("po_numbers") or "[]"))
        paid = action["cost_eur"] > 0
        if not paid and shipment_commitment_blocker(action["finding_id"], action["kind"], order_refs):
            return action
        if paid:
            unsupported = {m["channel"] for m in action["messages"] if m["channel"] not in {"email", "approval_email"}
                           and m["status"] in {"draft", "outbox"}}
            if unsupported:
                return actions.hold(action_id, "paid release requires email delivery; pending " + ", ".join(sorted(unsupported)))
        reply = None
        if action["finding_id"].startswith("reply-"):
            from core import inbox
            reply = inbox.get_reply(action["finding_id"].removeprefix("reply-"))
        pending = any(m["status"] in UNCONFIRMED and m["channel"] != "approval_email" for m in action["messages"])
        for m in action["messages"]:
            if m["channel"] == "approval_email" or m["status"] not in ("draft", "outbox"):
                continue
            if action["kind"] == "chase_escalation" and m["channel"] == "discord":
                continue
            if m["channel"] == "email" and action["kind"] not in {"chase_supplier", "chase_escalation"} and not paid and cfg("chase")["enabled"]:
                from core.chase_scope import duplicate_case
                with actions.tx() as c:
                    case_id = duplicate_case(c, action, m)
                if case_id:
                    if _mark(m["id"], "cancelled", expected=("draft", "outbox")):
                        actions.log_event("email_deduplicated", action_id, f"Supplier inquiry already covered by {case_id}.")
                    continue
            parent = None
            if reply and (m["to_address"] or "").casefold() == reply["from_addr"].casefold():
                ref = reply["message_id"]
                if ref.startswith("<") and ref.endswith(">"):
                    parent = ref
            elif action["kind"] == "chase_supplier" and (parent_id := action["payload"].get("parent_message_id")):
                with actions.tx() as c:
                    original = c.execute("SELECT provider_id,to_address FROM messages WHERE id=? AND status='sent'",
                                         (parent_id,)).fetchone()
                if not original or not original["provider_id"] or original["to_address"].casefold() != m["to_address"].casefold():
                    continue
                parent = original["provider_id"]
            try:
                if paid:
                    _verify_release(action_id)
                email_message = dict(m, body_en=None) if action["kind"] == "chase_supplier" else m
                status = send(email_message, parent)
                pending |= status in {"outbox", *UNCONFIRMED}
            except ReleaseHeld as exc:
                reason = str(exc)
                return actions.hold(action_id, reason, supersede=reason.startswith("duplicate of "))
            except (OSError, ValueError) as exc:
                pending = True
                if _mark(m["id"], "outbox", expected=("draft", "outbox")):
                    actions.log_event("dispatch_error", action_id, f"{m['subject']}: {type(exc).__name__}")
        if action["status"] == "scheduled":
            with actions.tx() as c:
                if not release.scheduled_authorized(c, action):
                    return action
        if action["status"] in ("approved", "scheduled") and not pending:
            return actions.set_status(action_id, "sent", entry)
        return actions.get(action_id)
