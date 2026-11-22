"""Open questions to Soony staff, one source for the dashboard, Discord and the answer endpoint.

A question is either a delivered Discord message Dockside sent to a person and nobody has answered yet, or a task
Dockside sets after a supplier contradicts a commitment (`task:<action>:<evidence>`). Questions asked before such a
contradiction are superseded: they stay in the history, but they can no longer be answered.
"""
from __future__ import annotations

import re

from core import actions, dispatch, inbox, rules


def _person(to_name: str | None) -> str:
    name = re.split(r"\s+(?:and|\()\s*", to_name or "", maxsplit=1)[0].strip()
    return rules.cfg("people").get(name) or name


def _rows(snapshot: dict) -> list[dict]:
    rows = []
    for part in snapshot.get("components", []):
        route = next((r for r in part.get("routes", []) if r["id"] == part.get("default_route_id")), None)
        for step in (route or {}).get("steps", []):
            if step.get("person"):
                rows.append(step | {"part_id": part["id"], "part": part.get("name")})
    return rows


def current(snapshot: dict | None = None) -> list[dict]:
    """Every answerable question: {id, person, text, part, kind}. Superseded questions are left out."""
    snapshot = snapshot or dispatch.snapshot(None)
    rows = _rows(snapshot)
    superseded = {row["superseded_message_id"] for row in rows if row.get("superseded_message_id")}
    out = [{"id": row["message_id"], "person": row["person"], "text": row["detail"], "part": row.get("part"), "kind": "task"}
           for row in rows if row.get("task") and row.get("message_id")]
    with actions.tx() as c:
        inbox.init_schema(c)
        for m in c.execute("""SELECT m.id, m.to_name, m.subject, m.body, a.title, a.finding_id FROM messages m JOIN actions a ON a.id=m.action_id
                              WHERE m.channel='discord' AND m.status='delivered' AND a.kind IN ('ask','escalate','chase_escalation')
                                AND NOT EXISTS (SELECT 1 FROM replies r WHERE r.msg_id=m.id)
                              ORDER BY julianday(m.sent_at) DESC, m.id"""):
            if m["id"] in superseded:
                continue
            out.append({"id": m["id"], "person": _person(m["to_name"]), "kind": "message",
                        "text": " ".join((m["subject"] or m["title"] or m["body"] or "").split())[:160],
                        "part": (actions.PART.search(m["finding_id"] or "") or [None])[0]})
    return out


def find(question_id: str, snapshot: dict | None = None) -> dict | None:
    """The question if it can still be answered; {'superseded': True, ...} if a later contradiction replaced it."""
    snapshot = snapshot or dispatch.snapshot(None)
    if match := next((q for q in current(snapshot) if q["id"] == question_id), None):
        return match
    for row in _rows(snapshot):
        if row.get("superseded_message_id") == question_id:
            return {"id": question_id, "person": row["person"], "superseded": True, "text": row["detail"]}
    with actions.tx() as c:  # a question not (yet) delivered or listed is still answerable by the person it was sent to
        inbox.init_schema(c)
        m = c.execute("""SELECT id, to_name, subject FROM messages WHERE id=?
                         AND NOT EXISTS (SELECT 1 FROM replies r WHERE r.msg_id=messages.id)""", (question_id,)).fetchone()
    if m:
        return {"id": m["id"], "person": _person(m["to_name"]), "kind": "message", "text": m["subject"] or "", "part": None}
    return None


def answer(question: dict, person: str, text: str) -> dict:
    if question["kind"] == "task":
        _, action_id, source = question["id"].split(":", 2)
        return inbox.add_task_answer(action_id, source, person, text, question["text"])
    return inbox.add_staff_answer(question["id"], person, text)
