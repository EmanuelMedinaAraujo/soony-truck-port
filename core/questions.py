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
# Stored answers use this text as a stable value.
CANCEL_ORDER = "I decided to cancel the order. Prepare the cancellation for approval."
POST_DEADLINE_SCHEMA = """CREATE TABLE IF NOT EXISTS post_deadline_tasks(
    task_id TEXT PRIMARY KEY REFERENCES staff_tasks(id), proposal_id TEXT)"""


def person(to_name: str | None) -> str:
    name = re.split(r"\s+(?:and|\()\s*", to_name or "", maxsplit=1)[0].strip()
    return release.role_owner(name) or rules.cfg("people").get(name) or (
        "Logistics Planner" if name == "owner_logistics" else "Buyer" if name.startswith("owner_") else name)


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
    assignee = person(task["person"])
    role = next((role for role, name in rules.cfg("people").items() if name == assignee), None)
    result = {key: task[key] for key in ("id", "status", "text", "answered_at")} | {
        "person": assignee, "person_role": role,
        "answer": task["answer_text"], "answered_by": reply["from_name"] if reply else None,
        "answerable_by": answerers(assignee, task=True),
    }
    if decision := _post_deadline_record(c, task["id"]):
        order = actions._get(c, task["action_id"])
        if plan := release.post_deadline_plan(order):
            sent = c.execute("""SELECT m.to_name, m.sent_at FROM staff_task_reminders t
                              JOIN messages m ON m.id=t.message_id
                              WHERE t.task_id=? AND m.subject=? AND m.channel='discord' AND m.status='delivered'
                                AND julianday(m.sent_at)>=julianday(?) ORDER BY julianday(m.sent_at) DESC""",
                             (task["id"], f"Reminder of task {task['id']}", task["created_at"]))
            reminded_at = next((row["sent_at"] for row in sent if person(row["to_name"]) == assignee), None)
            result.update(_decision_fields(order, plan), proposal_id=decision["proposal_id"], reminded_at=reminded_at)
    return result


def _post_deadline_record(c, task_id):
    if _has_table(c, "post_deadline_tasks"):
        return c.execute("SELECT * FROM post_deadline_tasks WHERE task_id=?", (task_id,)).fetchone()
    return None


def is_post_deadline(c, task_id):
    return _post_deadline_record(c, task_id) is not None


def _decision_fields(order, plan):
    policy = release.setting("late_collectors_ship_instead_of_refund", None)
    keep = "Keep the order for Monday delivery"
    cancel = "Cancel the order"
    recommendation = (
        "Keep the order if Monday delivery can replace the refunds."
        if policy is True else
        "Cancel the order because Monday delivery leaves the refunds due."
        if policy is False else
        "Confirm whether Monday delivery can replace the refunds before choosing."
    )
    price = f"€{release.delivery_terms(order)['total_price']:,.0f}"
    return {
        "kind": "post_deadline",
        "recommendation": recommendation,
        "options": [
            {"answer": "keep_monday", "label": keep,
             "consequence": (f"Up to {plan['late_copies']:,} Collector’s copies could ship after launch. "
                             f"The order costs {price}. Statues still leave {plan['remaining_statue_shortfall']:,} copies short.")},
            {"answer": "cancel", "label": cancel,
             "consequence": f"Avoids up to {price} if the supplier accepts the cancellation. "
                            f"All {plan['short_copies']:,} copies stay short."},
        ],
    }


def _decision_text(plan, delivery):
    deadline = rules.ts(delivery["departure"]["deadline"]).astimezone(rules.timezone())
    return (f"There is no proof the {plan['quantity']:,} art books left by {deadline:%H:%M} for Sunday. "
            "Decide whether to keep the order for Monday delivery or cancel it.")


def _switch_to_post_deadline(c, order, delivery, task, plan):
    task_id = f"task:{order['id']}:post-deadline"
    text = _decision_text(plan, delivery)
    owner = person(release.role_owner("owner_reconcile", order["approver_name"] or "Buyer"))
    deadline = delivery["departure"]["deadline"]
    if task and task["id"] != task_id:
        c.execute("UPDATE staff_tasks SET status='closed' WHERE id=?", (task["id"],))
    source = next((claim["message_id"] for claim in reversed(delivery["claims"])), "")
    c.execute("""INSERT INTO staff_tasks (id,action_id,source_reply_id,person,text,status,created_at)
                 VALUES (?,?,?,?,?,'open',?)
                 ON CONFLICT(id) DO UPDATE SET person=excluded.person,text=excluded.text,
                    status=CASE WHEN staff_tasks.answered_reply_id IS NULL THEN 'open' ELSE 'answered' END
                 WHERE staff_tasks.status='closed' OR staff_tasks.person<>excluded.person
                    OR staff_tasks.text<>excluded.text""",
              (task_id, order["id"], source, owner, text, deadline))
    c.execute("INSERT OR IGNORE INTO post_deadline_tasks(task_id) VALUES (?)", (task_id,))
    _attach(c, order, delivery["delivery_id"], task_id)


def _attach(c, order, delivery_id, task_id):
    for message in c.execute(MESSAGE_QUESTIONS + " AND COALESCE(t.task_id,'')<>?", (task_id,)):
        payload = json.loads(message["payload"] or "{}")
        if (person(message["to_name"]) != release.role_owner("owner_logistics")
                and _same_group(c, order, delivery_id, message["finding_id"], payload.get("delivery_id"))):
            c.execute("""INSERT INTO staff_task_reminders VALUES (?,?)
                         ON CONFLICT(message_id) DO UPDATE SET task_id=excluded.task_id""", (message["id"], task_id))


def _task_origin(c, order_id, claims):
    # Start a new task after the last dispute already covered by a closed task.
    closed_sources = {row["source_reply_id"] for row in c.execute(
        "SELECT source_reply_id FROM staff_tasks WHERE action_id=? AND status='closed'", (order_id,))}
    disputes = [claim for claim in claims if claim["kind"] == "disputed"]
    previous = max((i for i, claim in enumerate(disputes) if claim["message_id"] in closed_sources), default=-1)
    return disputes[min(previous + 1, len(disputes) - 1)]


def _reconcile_order(c, order, now):
    delivery, post = _delivery_for_task(c, order, now)
    conflict = delivery["conflict"]
    task = c.execute("SELECT * FROM staff_tasks WHERE action_id=? AND status<>'closed'", (order["id"],)).fetchone()
    if post:
        _switch_to_post_deadline(c, order, delivery, task, post)
        return
    if task and is_post_deadline(c, task["id"]):
        c.execute("UPDATE staff_tasks SET status='closed' WHERE id=?", (task["id"],))
        task = None
    if conflict:
        owner = person(conflict["owner_reconcile"])
        source = conflict["message_id"]
        if task is None:
            task = c.execute("""SELECT * FROM staff_tasks WHERE action_id=? AND source_reply_id=? AND status='closed'
                                AND id NOT IN (SELECT task_id FROM post_deadline_tasks)
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


def _delivery_for_task(c, order, now, depart_for_requested=None):
    delivery = release.delivery_state(order, now=now, depart_for_requested=depart_for_requested, log=c)
    if plan := release.post_deadline_plan(order):
        departure = release.sunday_recovery_departure(order, now, c)
        delivery.update(departure=departure, depart_by=departure["deadline"])
        if departure["state"] == "passed" and delivery["hold_reason"] != "evidence stale or unreadable":
            return delivery, plan
    return delivery, None


def order_state(order, *, now, depart_for_requested=None):
    """Read release evidence and the task already recorded by the background cycle."""
    with release._action_log() as c:
        c.execute("BEGIN")
        delivery, post = _delivery_for_task(c, order, now, depart_for_requested)
        if (delivery["conflict"] or post) and _has_table(c, "staff_tasks"):
            task = c.execute("SELECT * FROM staff_tasks WHERE action_id=? AND status<>'closed'", (order["id"],)).fetchone()
            if task:
                view = public(c, task)
                if bool(post) == (view.get("kind") == "post_deadline"):
                    delivery.update(task=view, task_created_at=delivery["departure"]["deadline"] if post else task["created_at"])
        return delivery


def reconcile():
    """Reconcile task records after the background cycle collects supplier evidence."""
    with actions.tx() as c:
        c.execute("BEGIN IMMEDIATE")
        refresh(c)


def refresh(c):
    """Discover disputed sent orders even when their offer is absent from the dashboard."""
    now = rules.now()
    c.execute(POST_DEADLINE_SCHEMA)
    _route_messages(c, now)
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


def _route_messages(c, now):
    for role, name in release.setting("role_owners", {}).items():
        rows = c.execute("SELECT id FROM messages WHERE channel='discord' AND to_name=?", (role,)).fetchall()
        for row in rows:
            c.execute("UPDATE messages SET to_name=? WHERE id=?", (name, row["id"]))
            c.execute("INSERT INTO events VALUES (?,?,?,?)",
                      (now.isoformat(), "message_reassigned", row["id"], f"{role} assigned to {name}."))


def for_action(c, finding, delivery_id, recipient) -> dict | None:
    if person(recipient) == release.role_owner("owner_logistics"):
        return None
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
    view = public(c, row)
    return view | {"kind": "task", "task_kind": view.get("kind"),
                          "part": (actions.PART.search(row["finding_id"] or "") or [None])[0],
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
            MESSAGE_QUESTIONS + """ AND (t.task_id IS NULL OR m.to_name=?) AND m.status='delivered'
                AND a.status NOT IN ('superseded','rejected','expired') ORDER BY julianday(m.sent_at) DESC, m.id""",
            (release.role_owner("owner_logistics"),))]
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
        if (task_id := message["reminder_of"]) and result["person"] != release.role_owner("owner_logistics"):
            result.update(superseded=True, reminder_of=task_id)
        elif message["action_status"] in {"superseded", "rejected", "expired"}:
            result.update(status="closed")
        return result
    return None


def _queue_post_deadline(c, target, reply, choice):
    order = actions._get(c, target["action_id"])
    sent = release.sent_order_message(order)
    prior = release.delivery_terms(order)
    finding = f"followup-{order['finding_id']}-{reply['id'].removeprefix('RPL-')}"
    task = c.execute("SELECT * FROM staff_tasks WHERE id=?", (target["id"],)).fetchone()
    message = {"channel": "email", "to_name": sent["to_name"], "to_address": sent["to_address"],
               "language": sent["language"], "subject": "", "body": ""}
    if choice == "cancel":
        cost, kind, title = 0, "cancel_order", "Request cancellation of the art-book order"
        terms = release.cancellation_terms(order, task)
    else:
        cost, kind, title = order["cost_eur"], "order", "Approve Monday delivery for the art-book order"
        deliver_by = release.monday_deliver_by(prior, release.post_deadline_plan(order))
        draft = prior | {"revises_action_id": order["id"], "deliver_by": deliver_by, "valid_until": deliver_by,
                         "post_deadline_task_id": task["id"], "post_deadline_reply_id": reply["id"]}
        terms = release.describe({"id": order["id"], "kind": kind, "finding_id": finding,
                                  "messages": [message], "payload": {"commitment": draft}}, parent=order)
    message["subject"], message["body"] = release.render_instruction(terms)
    rationale = ("The buyer chose to request cancellation after the Sunday departure deadline."
                 if choice == "cancel" else "The buyer chose to keep the original order for Monday delivery.")
    proposal = actions.propose(finding, title, rationale,
                               kind, cost, 0, 0, order["category"], target["person"],
                               payload={"commitment": terms}, messages=[message], parent=order, connection=c)
    c.execute("UPDATE post_deadline_tasks SET proposal_id=? WHERE task_id=?", (proposal["id"], task["id"]))
    c.execute("UPDATE replies SET handled=1,summary_en=? WHERE id=?",
              (f"Decision recorded. {title}.", reply["id"]))
    return proposal


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
            if target.get("task_kind") == "post_deadline":
                if text not in {option["answer"] for option in target["options"]}:
                    raise ValueError("Choose Monday delivery or cancellation for this order.")
                reply = inbox.add_task_answer(c, target, who, text, channel)
                proposal = _queue_post_deadline(c, target, reply, text)
                reply.update(proposal_id=proposal["id"], approval_requested=proposal["cost_eur"] > 0)
                return reply
            return inbox.add_task_answer(c, target, who, text, channel)
        return inbox.add_staff_answer(c, target, who, text, channel)
