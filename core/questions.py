"""Staff tasks and Discord questions shared by dispatch, the agent and both answer channels."""
from __future__ import annotations

import json
import re

from core import actions, inbox, release, rules

MESSAGE_QUESTIONS = """SELECT m.id, m.to_name, m.subject, m.body, m.sent_at, a.title, a.finding_id,
                              a.payload, a.status AS action_status, t.task_id AS reminder_of
                       FROM messages m JOIN actions a ON a.id=m.action_id
                       LEFT JOIN staff_task_reminders t ON t.message_id=m.id
                       WHERE m.channel='discord' AND a.kind IN ('ask','escalate','chase_escalation')
                         AND m.status IN ('delivered','draft','outbox')
                         AND NOT EXISTS (SELECT 1 FROM replies r WHERE r.msg_id=m.id)"""

ORDER_STANDS = "The order stands; I confirmed the dispatch instruction with the supplier in writing."
CANCEL_ORDER = "I decided to cancel the order. Prepare the cancellation for approval."


def person(to_name: str | None) -> str:
    name = re.split(r"\s+(?:and|\()\s*", to_name or "", maxsplit=1)[0].strip()
    return rules.cfg("people").get(name) or name


def _root(c, finding: str) -> str:
    visited = set()
    while finding not in visited:
        visited.add(finding)
        if finding.startswith("followup-"):
            finding = finding.removeprefix("followup-").rsplit("-", 1)[0]
        elif finding.startswith("reply-") and (row := c.execute(
            "SELECT a.finding_id FROM replies r JOIN actions a ON a.id=r.action_id WHERE r.id=?",
            (finding.removeprefix("reply-"),),
        ).fetchone()):
            finding = row["finding_id"] or ""
        else:
            break
    return finding


def _same_group(c, order, delivery, finding, delivery_id):
    if delivery_id:
        return delivery_id in {order["id"], delivery}
    root = _root(c, order["finding_id"] or "")
    return bool(root and _root(c, finding or "") == root)


def answerers(assignee: str, *, task: bool) -> list[str]:
    """Include named delegates and the assignee's procurement chain for order tasks."""
    allowed = [assignee, *release.setting("delegates", {}).get(assignee, [])]
    if task:
        people = rules.cfg("people")
        roles = [role for role, name in people.items() if name == assignee]
        for role in roles:
            if role.startswith("Buyer:"):
                allowed.append(people.get(role.replace("Buyer:", "Category Lead:", 1)))
        if any(role.startswith(("Buyer:", "Category Lead:")) for role in roles):
            allowed.append(people.get("Head of Procurement"))
        if any(role.startswith(("Buyer:", "Category Lead:")) or role == "Head of Procurement" for role in roles):
            allowed.append(people.get("CPO"))
    return list(dict.fromkeys(name for name in allowed if name))


def can_answer(question: dict, who: str) -> bool:
    return who in answerers(question["person"], task=question["kind"] == "task")


def public(c, task) -> dict:
    reply = (c.execute("SELECT from_name FROM replies WHERE id=?", (task["answered_reply_id"],)).fetchone()
             if task["answered_reply_id"] else None)
    return {key: task[key] for key in ("id", "status", "person", "text", "answered_at")} | {
        "answer": task["answer_text"], "answered_by": reply["from_name"] if reply else None,
        "answerable_by": answerers(task["person"], task=True),
    }


def _attach(c, order, delivery_id, task_id):
    for message in c.execute(MESSAGE_QUESTIONS + " AND t.task_id IS NULL"):
        payload = json.loads(message["payload"] or "{}")
        if _same_group(c, order, delivery_id, message["finding_id"], payload.get("delivery_id")):
            c.execute("INSERT OR IGNORE INTO staff_task_reminders VALUES (?,?)", (message["id"], task_id))


def _task_origin(c, order_id, claims):
    # Start a new task after the last dispute already covered by a closed task.
    closed_sources = {row["source_reply_id"] for row in c.execute(
        "SELECT source_reply_id FROM staff_tasks WHERE action_id=? AND status='closed'", (order_id,))}
    disputes = [claim for claim in claims if claim["kind"] == "disputed"]
    previous = max((i for i, claim in enumerate(disputes) if claim["message_id"] in closed_sources), default=-1)
    return disputes[min(previous + 1, len(disputes) - 1)]


def _reconcile_order(c, order, now):
    delivery = release.delivery_state(order, now=now, log=c)
    conflict = delivery["conflict"]
    task = c.execute("SELECT * FROM staff_tasks WHERE action_id=? AND status<>'closed'", (order["id"],)).fetchone()
    if conflict:
        owner = person(conflict["owner_reconcile"])
        source = conflict["message_id"]
        if task is None:
            task = c.execute("""SELECT * FROM staff_tasks WHERE action_id=? AND source_reply_id=? AND status='closed'
                                ORDER BY created_at DESC LIMIT 1""", (order["id"], source)).fetchone()
        at = rules.ts(conflict["at"]).astimezone(rules.timezone())
        text = (f'{conflict["who"]} wrote at {at:%a %H:%M}: “{conflict["text"].rstrip(".")}.” '
                "Reconcile the order reference, quantity, price and dispatch instruction with the supplier in writing. "
                "Record what was agreed.")
        origin = _task_origin(c, order["id"], delivery["claims"]) if task is None else None
        task_id = task["id"] if task else f"task:{order['id']}:{origin['message_id']}"
        created_at = task["created_at"] if task else origin["at"]
        c.execute("""INSERT INTO staff_tasks (id, action_id, source_reply_id, person, text, status, created_at)
                     VALUES (?,?,?,?,?,'open',?)
                     ON CONFLICT(id) DO UPDATE SET source_reply_id=excluded.source_reply_id, person=excluded.person,
                       text=excluded.text, status=CASE WHEN staff_tasks.answered_reply_id IS NULL THEN 'open' ELSE 'answered' END
                     WHERE staff_tasks.status='closed' OR staff_tasks.source_reply_id<>excluded.source_reply_id
                       OR staff_tasks.person<>excluded.person OR staff_tasks.text<>excluded.text""",
                  (task_id, order["id"], source, owner, text, created_at))
        if task is None:
            # Older deployments queued answers with this reply key before staff_tasks existed.
            c.execute("""UPDATE staff_tasks SET status='answered',
                         (answered_reply_id, answer_text, answered_at) = (
                             SELECT id, body, received_at FROM replies
                             WHERE action_id=staff_tasks.action_id AND message_id=staff_tasks.id||':'||id
                             ORDER BY julianday(received_at), id LIMIT 1)
                         WHERE id=? AND answered_reply_id IS NULL AND EXISTS (
                             SELECT 1 FROM replies WHERE action_id=staff_tasks.action_id AND message_id=staff_tasks.id||':'||id)""", (task_id,))
        _attach(c, order, delivery["delivery_id"], task_id)
    elif task and delivery["hold_reason"] != "evidence stale or unreadable":
        c.execute("UPDATE staff_tasks SET status='closed' WHERE id=?", (task["id"],))


def _has_table(c, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def order_state(order, *, now, depart_for_requested=None):
    """Read release evidence and the task already recorded by the background cycle."""
    with release._action_log() as c:
        c.execute("BEGIN")
        delivery = release.delivery_state(order, now=now, depart_for_requested=depart_for_requested, log=c)
        if delivery["conflict"] and _has_table(c, "staff_tasks"):
            task = c.execute("SELECT * FROM staff_tasks WHERE action_id=? AND status<>'closed'", (order["id"],)).fetchone()
            if task:
                delivery.update(task=public(c, task), task_created_at=task["created_at"])
        return delivery


def reconcile():
    """Reconcile task records after the background cycle collects supplier evidence."""
    with actions.tx() as c:
        c.execute("BEGIN IMMEDIATE")
        refresh(c)


def refresh(c):
    """Discover disputed sent orders even when their offer is absent from the dashboard."""
    now = rules.now()
    orders = c.execute("""SELECT a.id FROM actions a WHERE a.kind='order'
        AND a.status NOT IN ('rejected','superseded','expired')
        AND json_extract(a.payload,'$.repeat_commitment') IS NULL
        AND EXISTS (SELECT 1 FROM messages m WHERE m.action_id=a.id AND m.channel='email'
                    AND m.status IN ('sent','delivered') AND m.sent_at IS NOT NULL)""")
    for row in orders.fetchall():
        _reconcile_order(c, actions._get(c, row["id"]), now)
    c.execute("""UPDATE staff_tasks SET status='closed' WHERE status<>'closed'
                 AND action_id IN (SELECT id FROM actions WHERE status IN ('rejected','superseded','expired'))""")


def reminders() -> dict[str, str]:
    with release._action_log() as c:
        if not _has_table(c, "staff_task_reminders"):
            return {}
        return dict(c.execute("SELECT message_id, task_id FROM staff_task_reminders"))


def for_action(c, finding, delivery_id, _recipient) -> dict | None:
    for task in c.execute("SELECT * FROM staff_tasks WHERE status<>'closed' ORDER BY created_at DESC, id"):
        order = actions._get(c, task["action_id"])
        terms = release.delivery_terms(order)
        if terms and _same_group(c, order, terms["delivery_id"], finding, delivery_id):
            return public(c, task)
    return None


def answered_replies() -> dict:
    if not actions.DB.is_file():
        return {}
    with actions.read() as c:
        return {row["answered_reply_id"]: public(c, row) for row in c.execute(
            "SELECT * FROM staff_tasks WHERE answered_reply_id IS NOT NULL")}


def _task_question(c, row) -> dict:
    return public(c, row) | {"kind": "task", "part": (actions.PART.search(row["finding_id"] or "") or [None])[0],
                          "asked_at": row["created_at"], "source_reply_id": row["source_reply_id"],
                          "action_id": row["action_id"]}


def _message_question(row) -> dict:
    return {"id": row["id"], "person": person(row["to_name"]), "kind": "message",
            "text": " ".join((row["subject"] or row["title"] or row["body"] or "").split())[:160],
            "part": (actions.PART.search(row["finding_id"] or "") or [None])[0], "asked_at": row["sent_at"]}


TASKS = """SELECT t.*, a.finding_id FROM staff_tasks t JOIN actions a ON a.id=t.action_id"""


def current() -> list[dict]:
    """Return each answerable task and delivered Discord question with its ID."""
    with actions.tx() as c:
        c.execute("BEGIN IMMEDIATE")
        refresh(c)
        tasks = [_task_question(c, row) for row in c.execute(TASKS + " WHERE t.status='open' ORDER BY t.created_at DESC")]
        messages = [_message_question(row) for row in c.execute(
            MESSAGE_QUESTIONS + """ AND t.task_id IS NULL AND m.status='delivered'
                AND a.status NOT IN ('superseded','rejected','expired') ORDER BY julianday(m.sent_at) DESC, m.id""")]
    return tasks + messages


def find(question_id: str) -> dict | None:
    with actions.tx() as c:
        c.execute("BEGIN IMMEDIATE")
        refresh(c)
        return _find(c, question_id)


def _find(c, question_id):
    if task := c.execute(TASKS + " WHERE t.id=?", (question_id,)).fetchone():
        return _task_question(c, task)
    if message := c.execute(MESSAGE_QUESTIONS + " AND m.id=?", (question_id,)).fetchone():
        result = _message_question(message)
        if task_id := message["reminder_of"]:
            result.update(superseded=True, reminder_of=task_id)
        elif message["action_status"] in {"superseded", "rejected", "expired"}:
            result.update(status="closed")
        return result
    return None


def answer(question: dict, who: str, text: str, *, channel: str = "dashboard", demo_operator: bool = False) -> dict:
    with actions.tx() as c:
        c.execute("BEGIN IMMEDIATE")
        refresh(c)
        target = _find(c, question["id"])
        if not target:
            raise KeyError(question["id"])
        if (target["person"] != question["person"] or target.get("superseded") or
                target.get("status", "open") != "open"):
            raise ValueError("This question is no longer open. Reload for the current task.")
        if demo_operator:
            who = f"{target['person']} (entered by the local demo operator)"
        elif not can_answer(target, who):
            raise PermissionError(f"This question is for {target['person']}.")
        if target["kind"] == "task":
            return inbox.add_task_answer(c, target, who, text, channel)
        return inbox.add_staff_answer(c, target, who, text, channel)
