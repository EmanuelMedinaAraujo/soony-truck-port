"""Native Supplier Chaser. The server cycle owns delivery, and SQLite owns case identity."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import asdict
from email.utils import parseaddr

from core import actions, chase_mail, inbox, llm_health, outbound, rules
from core.chase_evidence import Evidence, Issue, detect


SUPPLIER_KIND = "chase_supplier"
ESCALATION_KIND = "chase_escalation"
ACTIVE = ("open", "waiting", "answered", "escalated")
TRANSITIONS = {
    "open": {"waiting", "answered", "resolved", "escalated", "cancelled"},
    "waiting": {"answered", "resolved", "escalated", "cancelled"},
    "answered": {"resolved", "escalated", "cancelled"},
    "escalated": {"answered", "resolved", "cancelled"},
    "resolved": set(),
    "cancelled": set(),
}
CASE_FIELDS = ("supplier_id", "po_number", "po_line", "material")


def _stamp():
    return rules.now().isoformat(timespec="seconds")


def case_id(key, kind):
    identity = json.dumps([*key, kind], separators=(",", ":"), ensure_ascii=False)
    return "CASE-" + hashlib.sha256(identity.encode()).hexdigest()[:24].upper()


def _event(c, cid, kind, detail, evidence=()):
    c.execute("INSERT INTO chase_events(case_id,ts,kind,detail,evidence) VALUES (?,?,?,?,?)",
              (cid, _stamp(), kind, detail, json.dumps([asdict(item) for item in evidence], ensure_ascii=False)))


def _move(c, cid, status, reason, evidence=()):
    old = c.execute("SELECT status FROM chase_cases WHERE case_id=?", (cid,)).fetchone()[0]
    if old == status or status not in TRANSITIONS[old]:
        return
    if status == "resolved" and not evidence:
        raise ValueError("A resolution requires evidence")
    c.execute("UPDATE chase_cases SET status=?,updated_at=? WHERE case_id=?", (status, _stamp(), cid))
    _event(c, cid, status, reason, evidence)


def _open(c, line, issue):
    cid = case_id(line.key, issue.kind)
    timestamp = _stamp()
    inserted = c.execute("""INSERT OR IGNORE INTO chase_cases
        (case_id,supplier_id,supplier_name,po_number,po_line,material,issue_kind,status,question,needs_review,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,'open',?,?,?,?)""",
        (cid, line.key[0], line.supplier.get("name") or line.key[0], *line.key[1:], issue.kind,
         issue.question, issue.review, timestamp, timestamp)).rowcount
    if inserted:
        _event(c, cid, "opened", issue.question, issue.evidence)
    else:
        known = {(item["path"], item["quote"]) for row in c.execute(
            "SELECT evidence FROM chase_events WHERE case_id=? AND kind IN ('opened','evidence')", (cid,))
                 for item in json.loads(row["evidence"])}
        new_evidence = tuple(e for e in issue.evidence if (e.path, e.quote) not in known)
        if new_evidence:
            _event(c, cid, "evidence", issue.question, new_evidence)
            c.execute("UPDATE chase_cases SET updated_at=?, needs_review=MAX(needs_review,?) WHERE case_id=?",
                      (timestamp, issue.review, cid))
    return cid, bool(inserted)


def _key(row):
    return tuple(row[k] for k in CASE_FIELDS)


def _opening(line, cid, issue):
    return {"case_id": cid, "kind": issue.kind, "supplier": line.supplier.get("name") or line.key[0],
            "po_number": line.key[1], "po_line": line.key[2], "needs_review": issue.review}


def _active(c):
    return c.execute("""SELECT * FROM chase_cases WHERE status IN ('open','waiting','answered','escalated')
                        ORDER BY case_id""").fetchall()


def _link(c, cid, mid, purpose, direction="outbound"):
    return c.execute("INSERT OR IGNORE INTO chase_case_messages VALUES (?,?,?,?)",
                     (cid, mid, direction, purpose)).rowcount


def _record(c, snapshot, report):
    lines = {line.key: line for line in snapshot.lines}
    for line in snapshot.lines:
        for issue in line.issues.values():
            cid, inserted = _open(c, line, issue)
            if inserted:
                report["opened"].append(_opening(line, cid, issue))
    for row in _active(c):
        key = _key(row)
        po, line = snapshot.orders.get(key), lines.get(key)
        # An absent row might be a partial portal drop. Require positive evidence to close a case.
        if po and po["status"] in {"delivered", "cancelled"}:
            _move(c, row["case_id"], "cancelled", f"The PO is {po['status']}.",
                  (Evidence(po["source"] or "purchase_orders", f"{po['po_number']} line {po['po_line']}: {po['status']}"),))
        elif line and line.adequate and row["issue_kind"] != "reply_follow_up":
            _move(c, row["case_id"], "resolved", "Current supplier evidence satisfies the PO line.",
                  line.confirmation_evidence)
    return lines


def _address(value):
    return bool(value and parseaddr(value)[1] == value and re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", value))


def _existing_message(c, line, snapshot):
    """Find an open supplier email with explicit PO scope or a matching shipment reference."""
    po_number = line.key[1]
    address = line.supplier.get("contact_email")
    if not _address(address):
        return None
    rows = c.execute("""SELECT a.finding_id,a.payload,a.id AS action_id,m.id AS message_id,m.subject,m.body,m.body_en,m.po_numbers
      FROM actions a JOIN messages m ON m.action_id=a.id
      WHERE a.status IN ('proposed','scheduled','approved','sent','escalated','auto')
        AND a.kind NOT IN ('chase_supplier','chase_escalation')
        AND m.channel='email' AND lower(m.to_address)=lower(?) AND m.status IN ('draft','outbox','sending','sent','delivery_unknown')
      ORDER BY a.created_at DESC, m.created_at DESC, m.id""", (address,))
    token = re.compile(r"(?<!\w)" + re.escape(po_number) + r"(?!\w)")
    for row in rows:
        if "[sc:" in (row["subject"] or "").casefold():
            continue
        if po_number in json.loads(row["po_numbers"] or "[]"):
            return row
        if any(token.search(value or "") for value in (row["subject"], row["body"], row["body_en"])):
            return row
        if po_number in snapshot.shipment_orders.get(row["finding_id"], ()):
            return row
        # Older actions did not store PO scope. Details are safe to use for one supplier only.
        payload = json.loads(row["payload"] or "{}")
        if row["po_numbers"] is None and token.search(payload.get("details") or "") and c.execute(
            "SELECT COUNT(*) FROM messages WHERE action_id=? AND channel='email'",
            (row["action_id"],)).fetchone()[0] == 1:
            return row
    return None


def _groups(c, lines):
    groups = defaultdict(list)
    for row in _active(c):
        if _key(row) in lines:
            groups[_key(row)[:2]].append(row)
    return groups


def _threads(c):
    return {(r["supplier_id"], r["po_number"]): r for r in c.execute("SELECT * FROM chase_threads")}


def _linked_cases(c, mid):
    return c.execute("""SELECT c.* FROM chase_cases c JOIN chase_case_messages m USING(case_id)
                        WHERE m.message_id=? AND m.direction='outbound'""", (mid,)).fetchall()


def _attach(c, cases, mid, purpose):
    for row in cases:
        if _link(c, row["case_id"], mid, purpose):
            _event(c, row["case_id"], purpose, f"Linked to email {mid}.")


def _attachment(c, key, cases, lines, snapshot, report):
    row = _existing_message(c, lines[_key(cases[0])], snapshot)
    if row is None:
        return False
    c.execute("INSERT INTO chase_threads VALUES (?,?,?,?,1,NULL,NULL)", (*key, row["action_id"], row["message_id"]))
    _attach(c, cases, row["message_id"], "existing_agent_email")
    report["attached"].append({"supplier_id": key[0], "po_number": key[1], "action_id": row["action_id"]})
    return True


def _prepare(c, snapshot, report):
    with c:
        c.execute("BEGIN IMMEDIATE")
        lines = _record(c, snapshot, report)
        threads = _threads(c)
        for key, cases in _groups(c, lines).items():
            if thread := threads.get(key):
                _attach(c, cases, thread["initial_message_id"], "same_po_thread")
            else:
                _attachment(c, key, cases, lines, snapshot, report)
    return lines


def _reconcile_replies(c, lines, report, *, dry_run):
    replies = c.execute("""SELECT r.* FROM replies r WHERE EXISTS(
        SELECT 1 FROM chase_case_messages m JOIN chase_cases cc USING(case_id)
        WHERE m.message_id=r.msg_id AND m.direction='outbound')
        ORDER BY COALESCE(r.received_at,''),r.rowid""").fetchall()
    for reply in replies:
        if "[sc:" in (reply["subject"] or "").casefold():
            continue
        cases = _linked_cases(c, reply["msg_id"])
        if not cases:
            continue
        message = c.execute("""SELECT m.to_address,m.provider_id,a.kind FROM messages m JOIN actions a ON a.id=m.action_id
                               WHERE m.id=? AND a.kind<>'chase_escalation'""", (reply["msg_id"],)).fetchone()
        if not message or not message["provider_id"] or (message["to_address"] or "").casefold() != reply["from_addr"].casefold():
            continue
        with c:
            c.execute("BEGIN IMMEDIATE")
            pending = []
            for row in _linked_cases(c, reply["msg_id"]):
                cid = row["case_id"]
                active = row["status"] in ACTIVE and _key(row) in lines
                if _link(c, cid, reply["id"], "received" if active else "recorded", "inbound"):
                    evidence = (Evidence(f"mailbox/{reply['id']}", reply["body"][:6000]),)
                    _move(c, cid, "answered", "A correlated supplier reply was received.", evidence)
                    _event(c, cid, "reply_received", reply["subject"], evidence)
                if active and c.execute("""SELECT 1 FROM chase_case_messages WHERE case_id=? AND message_id=?
                                           AND direction='inbound' AND purpose='received'""", (cid, reply["id"])).fetchone():
                    pending.append(row)
        if not pending:
            continue
        if dry_run:
            report["replies_pending"] += 1
            continue
        applicable = {row["po_line"]: lines[_key(row)] for row in pending}
        try:
            result = chase_mail.interpret(dict(reply), list(applicable.values()))
        except llm_health.LLMUnavailable:
            report["replies_pending"] += 1
            continue
        except Exception:
            result = {"lines": [], "summary": "The supplier reply needs review."}
        claims = defaultdict(list)
        for claim in result.get("lines") or []:
            if isinstance(claim, dict) and isinstance(claim.get("line"), int):
                claims[claim["line"]].append(claim)
        summary = " ".join(str(result.get("summary") or "Supplier reply received.").split())[:300]
        multiple = len({_key(r) for r in _linked_cases(c, reply["msg_id"])}) > 1
        with c:
            c.execute("BEGIN IMMEDIATE")
            follow_ups = []
            for row in pending:
                cid = row["case_id"]
                applied = c.execute("""UPDATE chase_case_messages SET purpose='reconciled'
                    WHERE case_id=? AND message_id=? AND direction='inbound' AND purpose='received'""",
                    (cid, reply["id"])).rowcount
                if not applied:
                    continue
                line = lines[_key(row)]
                candidates = claims[row["po_line"]]
                claim = candidates[0] if len(candidates) == 1 else {}
                verified = chase_mail.verified_claim(claim, reply, line, multiple)
                qty, arrival, rev, quote = verified or (None, None, None, reply["body"][:6000])
                evidence = (Evidence(f"mailbox/{reply['id']}", quote),)
                supported = line.need and qty == line.qty and arrival and arrival <= line.need
                if line.revision or row["issue_kind"] == "contradicting_revision":
                    supported = supported and rev and (line.revision is None or rev == line.revision)
                c.execute("UPDATE chase_cases SET last_reply_summary=?,updated_at=? WHERE case_id=?", (summary, _stamp(), cid))
                if claim.get("intent") == "confirmed" and supported:
                    _move(c, cid, "resolved", "The supplier confirmed the ordered quantity, timing and any required revision.", evidence)
                else:
                    _move(c, cid, "escalated", "Supplier reply does not settle the PO discrepancy.", evidence)
                    issue = Issue("reply_follow_up", "Review the supplier reply and decide how to recover the outstanding commitment.",
                                  (line.evidence, *evidence), review=True)
                    followup, opened = _open(c, line, issue)
                    _link(c, followup, reply["id"], "reconciled", "inbound")
                    c.execute("UPDATE chase_cases SET last_reply_summary=?,updated_at=? WHERE case_id=?", (summary, _stamp(), followup))
                    _event(c, followup, "follow_up", f"Follow-up for {cid}.", evidence)
                    if opened:
                        report["opened"].append(_opening(line, followup, issue))
                        follow_ups.append(c.execute("SELECT * FROM chase_cases WHERE case_id=?", (followup,)).fetchone())
                report["replies_processed"] += 1
            if follow_ups:
                _escalate(c, _key(follow_ups[0])[:2], follow_ups, lines, "The supplier's reply needs a buyer decision.", report)
            if message["kind"] == SUPPLIER_KIND:
                c.execute("UPDATE replies SET handled=1,summary_en=? WHERE id=?", (summary, reply["id"]))


def _factual_lines(cases, lines):
    review_keys = {_key(row) for row in cases if row["needs_review"] and row["issue_kind"] != "reply_follow_up"}
    eligible = [row for row in cases if (line := lines.get(_key(row))) and not row["needs_review"]
                and _key(row) not in review_keys and "needs_review" not in line.issues
                and row["issue_kind"] in line.issues]
    kinds = defaultdict(list)
    for row in eligible:
        kinds[_key(row)].append(row["issue_kind"])
    return eligible, [(line, kinds[key]) for key, line in lines.items() if key in kinds and _address(line.supplier.get("contact_email"))]


def _queue(c, kind, key, cases, owner, messages, parent=None):
    titles = {"initial": "Confirm purchase order delivery", "reminder": "Follow up on purchase order delivery",
              "escalation": "Review unanswered purchase order questions"}
    code = ESCALATION_KIND if kind == "escalation" else SUPPLIER_KIND
    if not actions.policy_automatic(code, 0):
        raise ValueError(f"Autonomy policy must enable {code}")
    action = actions.propose(
        f"chase-{kind}-{key[0]}-{key[1]}", titles[kind], "Clarify purchase order commitments for the launch.",
        code, 0, 0, 0, None, owner, payload={"case_ids": [r["case_id"] for r in cases], "po_number": key[1],
                                           "supplier_id": key[0], "parent_message_id": parent}, messages=messages, connection=c,
    )
    for row in cases:
        if action["messages"]:
            _attach(c, [row], action["messages"][0]["id"], kind + "_queued")
        else:
            _event(c, row["case_id"], "no_internal_address", f"{owner} has no configured internal email address.")
    return action


def _escalate(c, key, cases, lines, reason, report):
    people = rules.cfg("people")
    settings = rules.cfg("chase")
    buyers = defaultdict(list)
    for row in cases:
        line = lines[_key(row)]
        for tier in rules.cfg("approval_ladder"):
            role = tier["role"]
            name = people.get(f"{role}:{line.category}") or people.get(role)
            if name:
                buyers[name].append(row)
                break
        else:
            buyers["Procurement"].append(row)
    for owner, owned_cases in buyers.items():
        address = settings.get("internal_emails", {}).get(owner)
        message = {"channel": "email", "to_name": owner, "to_address": address, "language": "en",
                   "subject": f"Dockside escalation for purchase order {key[1]}",
                   "body": f"{reason}\n\nPlease review "
                           + ", ".join(f"{lines[_key(r)].name} ({r['case_id']})" for r in owned_cases) + "."}
        action = _queue(c, "escalation", key, owned_cases, owner, [message] if _address(address) else [])
        for row in owned_cases:
            _move(c, row["case_id"], "escalated", reason)
            _event(c, row["case_id"], "internal_escalation", f"Escalated to {owner} via {action['id']}.")
        report["actions"].append(action["id"])
        report["escalations"].append({"po_number": key[1], "owner": owner, "email": bool(action["messages"])})


def _message(c, mid):
    return c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()


def _follow_ups(c, key, thread, cases, lines, snapshot, report, *, dry_run):
    initial = _message(c, thread["initial_message_id"])
    if not initial:
        return
    latest = _message(c, thread["reminder_message_id"]) or initial
    wait = dt.timedelta(hours=rules.cfg("chase")["reminder_after_hours"])
    eligible, _ = _factual_lines(cases, lines)
    waiting = [r for r in eligible if r["status"] in {"open", "waiting"}]
    if not waiting:
        return
    with c:
        c.execute("BEGIN IMMEDIATE")
        if latest["status"] in {"sending", "delivery_unknown"} and rules.now() >= dt.datetime.fromisoformat(latest["created_at"]) + wait:
            _escalate(c, key, waiting, lines, "Email delivery status is uncertain. Check the thread before contacting the supplier.", report)
            return
        if latest["status"] == "cancelled":
            _escalate(c, key, waiting, lines, "The queued inquiry was cancelled after the PO evidence changed.", report)
            return
        if initial["status"] == "sent":
            for row in waiting:
                _move(c, row["case_id"], "waiting", "A supplier email was sent.")
        if latest["status"] != "sent" or not latest["sent_at"] or rules.now() < dt.datetime.fromisoformat(latest["sent_at"]) + wait:
            return
        if thread["reminder_message_id"] or thread["attached"] or _existing_message(c, lines[_key(waiting[0])], snapshot):
            _escalate(c, key, waiting, lines, "The supplier has not answered the PO inquiry.", report)
            return
    if rules.cfg("chase")["only_launch_relevant"] and rules.now() >= rules.ts(rules.cfg("deadline")):
        return
    covered, line_items = _factual_lines(waiting, lines)
    if not line_items:
        return
    # Keep a reminder on the original email thread and only use the current trusted contact.
    if line_items[0][0].supplier["contact_email"].casefold() != initial["to_address"].casefold():
        with c:
            _escalate(c, key, covered, lines, "The supplier contact changed. Review the existing thread.", report)
        return
    message = chase_mail.draft(line_items, reminder=True, use_model=not dry_run,
                               address=initial["to_address"], subject=initial["subject"])
    with c:
        c.execute("BEGIN IMMEDIATE")
        current = _threads(c).get(key)
        if current["reminder_message_id"] or _existing_message(c, line_items[0][0], snapshot):
            return
        action = _queue(c, "reminder", key, covered, "Dockside", [message], parent=initial["id"])
        c.execute("UPDATE chase_threads SET reminder_action_id=?,reminder_message_id=? WHERE supplier_id=? AND po_number=?",
                  (action["id"], action["messages"][0]["id"], *key))
    report["actions"].append(action["id"])
    report["mail"].append({"supplier": line_items[0][0].supplier.get("name") or key[0], "po_number": key[1], "kind": "reminder"})


def _start_threads(c, groups, lines, snapshot, report, *, dry_run):
    settings = rules.cfg("chase")
    if settings["only_launch_relevant"] and rules.now() >= rules.ts(rules.cfg("deadline")):
        return
    remaining = settings["max_new_threads_per_cycle"]
    for key in dict.fromkeys(line.key[:2] for line in lines.values()):
        if remaining <= 0:
            break
        cases = [row for row in groups.get(key, []) if row["status"] == "open"]
        if not cases or key in _threads(c):
            continue
        covered, line_items = _factual_lines(cases, lines)
        if not line_items:
            continue
        message = chase_mail.draft(line_items, use_model=not dry_run)
        with c:
            c.execute("BEGIN IMMEDIATE")
            if key in _threads(c) or _attachment(c, key, cases, lines, snapshot, report):
                continue
            action = _queue(c, "initial", key, covered, "Dockside", [message])
            c.execute("INSERT INTO chase_threads VALUES (?,?,?,?,0,NULL,NULL)",
                      (*key, action["id"], action["messages"][0]["id"]))
        remaining -= 1
        report["actions"].append(action["id"])
        report["mail"].append({"supplier": line_items[0][0].supplier.get("name") or key[0], "po_number": key[1], "kind": "initial"})


def _dispatch_pending(c, lines, snapshot, report, *, dry_run):
    pending = c.execute("""SELECT m.id,m.action_id,m.to_address,m.subject,a.kind,a.payload FROM messages m
        JOIN actions a ON a.id=m.action_id WHERE a.status='auto' AND a.kind IN ('chase_supplier','chase_escalation')
        AND m.status IN ('draft','outbox') ORDER BY m.created_at,m.rowid""").fetchall()
    for row in pending:
        if row["kind"] == SUPPLIER_KIND:
            cases = _linked_cases(c, row["id"])
            all_active = [r for r in _active(c) if _key(r)[:2] == _key(cases[0])[:2]] if cases else []
            relevant, _ = _factual_lines(all_active, lines)
            safe = {r["case_id"] for r in relevant if r["status"] in {"open", "waiting"} and _key(r) in lines
                    and (lines[_key(r)].supplier.get("contact_email") or "").casefold() == row["to_address"].casefold()}
            if (not cases or any(r["case_id"] not in safe for r in cases)
                or (rules.cfg("chase")["only_launch_relevant"] and rules.now() >= rules.ts(rules.cfg("deadline")))):
                with c:
                    c.execute("UPDATE messages SET status='cancelled' WHERE id=? AND status IN ('draft','outbox')", (row["id"],))
                    for r in cases:
                        _event(c, r["case_id"], "email_cancelled", "The queued inquiry is no longer current.")
                continue
            existing = _existing_message(c, lines[_key(cases[0])], snapshot)
            if existing:
                with c:
                    c.execute("UPDATE messages SET status='cancelled' WHERE id=? AND status IN ('draft','outbox')", (row["id"],))
                    _attach(c, cases, existing["message_id"], "existing_agent_email")
                    thread = _threads(c)[_key(cases[0])[:2]]
                    if thread["initial_message_id"] == row["id"]:
                        c.execute("""UPDATE chase_threads SET initial_action_id=?,initial_message_id=?,attached=1
                                     WHERE supplier_id=? AND po_number=?""",
                                  (existing["action_id"], existing["message_id"], *_key(cases[0])[:2]))
                continue
        if dry_run:
            if row["action_id"] not in report["actions"]:
                report["queued_mail"].append({"action_id": row["action_id"], "subject": row["subject"]})
            continue
        sent = outbound.dispatch(row["action_id"])
        if row["kind"] == SUPPLIER_KIND and sent and any(m["status"] == "sent" for m in sent["messages"]):
            with c:
                for case in _linked_cases(c, row["id"]):
                    _move(c, case["case_id"], "waiting", "A supplier email was sent.")


def _connection(dry_run):
    if not dry_run:
        return actions.con()
    # Work on a snapshot so the dry run exercises exactly the same bookkeeping without persisting it.
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    if actions.DB.is_file():
        with closing(sqlite3.connect(actions.DB.as_uri() + "?mode=ro", uri=True)) as source:
            source.backup(c)
    actions.init_schema(c)
    return c


def run(*, dry_run=False):
    report = {"enabled": rules.cfg("chase")["enabled"], "opened": [], "attached": [], "mail": [], "escalations": [],
              "queued_mail": [], "replies_pending": 0, "replies_processed": 0, "actions": []}
    if not dry_run and not report["enabled"]:
        return report
    snapshot = detect()
    with closing(_connection(dry_run)) as c:
        c.execute(inbox.REPLY_SCHEMA)
        lines = _prepare(c, snapshot, report)
        _reconcile_replies(c, lines, report, dry_run=dry_run)
        groups = _groups(c, lines)
        for key, thread in _threads(c).items():
            _follow_ups(c, key, thread, groups.get(key, []), lines, snapshot, report, dry_run=dry_run)
        _start_threads(c, groups, lines, snapshot, report, dry_run=dry_run)
        _dispatch_pending(c, lines, snapshot, report, dry_run=dry_run)
    report["count_by_kind"] = dict(sorted(Counter(row["kind"] for row in report["opened"]).items()))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", required=True, help="Preview the cycle without changing data or sending email.")
    parser.parse_args()
    report = run(dry_run=True)
    del report["actions"]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
