"""Read the native case log for Dockside's API and agent tools."""
from __future__ import annotations

import json
from contextlib import closing

from core import actions, inbox

SUMMARY = ("case_id", "supplier_id", "supplier_name", "po_number", "material", "status", "question",
           "last_reply_summary", "updated_at")


def _rows(c, supplier_id=None, po_number=None, active_only=False):
    filters, args = [], []
    if supplier_id:
        filters.append("supplier_id=?")
        args.append(supplier_id)
    if po_number:
        filters.append("po_number=?")
        args.append(po_number)
    if active_only:
        filters.append("status IN ('open','waiting','answered','escalated')")
    where = " WHERE " + " AND ".join(filters) if filters else ""
    return c.execute("SELECT * FROM chase_cases" + where + " ORDER BY updated_at DESC,case_id", args)


def cases(*, include_messages=True, supplier_id=None, po_number=None, active_only=False):
    with closing(actions.con()) as c:
        result = []
        for row in _rows(c, supplier_id, po_number, active_only):
            case = {k: row[k] for k in SUMMARY}
            if row["status"] in {"resolved", "cancelled"}:
                case["status"] = "closed"
            if not include_messages:
                case["question"] = case["last_reply_summary"] = None
            result.append(case)
    return result, "ok"


def list_native():
    with closing(actions.con()) as c:
        return [dict(row) for row in _rows(c)]


def detail(case_id):
    with closing(actions.con()) as c:
        row = c.execute("SELECT * FROM chase_cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            return None
        c.execute(inbox.REPLY_SCHEMA)
        case = dict(row)
        events = [dict(r, evidence=json.loads(r["evidence"])) for r in c.execute(
            "SELECT * FROM chase_events WHERE case_id=? ORDER BY id", (case_id,))]
        messages = [dict(m) for m in c.execute("""SELECT m.id,m.action_id,cm.direction,cm.purpose,m.subject,m.body,m.body_en,
                         m.status,m.to_name,m.provider_id,COALESCE(m.sent_at,m.created_at) AS ts
            FROM chase_case_messages cm JOIN messages m ON m.id=cm.message_id
            WHERE cm.case_id=? AND cm.direction='outbound'
            UNION ALL
            SELECT r.id,r.action_id,cm.direction,cm.purpose,r.subject,r.body,NULL,'received',r.from_name,r.message_id,r.received_at
            FROM chase_case_messages cm JOIN replies r ON r.id=cm.message_id
            WHERE cm.case_id=? AND cm.direction='inbound'
            ORDER BY ts,id""", (case_id, case_id))]
        timeline = [dict(event, type="event") for event in events] + [dict(msg, type="message") for msg in messages]
        timeline.sort(key=lambda item: item["ts"] or "")
        return {**case, "events": events, "messages": messages, "timeline": timeline}
