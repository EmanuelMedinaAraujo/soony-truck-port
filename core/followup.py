"""Nothing waits silently: items with no progress get a follow-up from Dockside.

A part's next step that is waiting on someone (a supplier reply or an overdue pickup) is reviewed again once it has been
quiet for `followup.after_hours`, sooner if a deadline comes first. The agent then takes the next step itself: a reminder, a dock slot, a question about a pickup, or an escalation. Each review is logged, so an item is reviewed at most
once per interval, also when the model call fails.
"""
from __future__ import annotations

import datetime as dt
import json
from contextlib import closing

from core import actions, release, rules

# Booked (pickup still ahead), Asking (Dockside is sending or reading) and On the way are not stalls.
WAITING = {"Waiting for reply", "Late", "Stuck", "Needs you"}
REVIEW_KIND = "followup_review"


def settings() -> dict:
    return {"enabled": False, "after_hours": 2, "max_per_cycle": 3} | dict(rules.cfg("followup"))


def last_reviews() -> dict[str, dt.datetime]:
    with closing(actions.con()) as c:
        rows = c.execute("SELECT ref, MAX(ts) FROM events WHERE kind=? GROUP BY ref", (REVIEW_KIND,)).fetchall()
    return {ref: stamp for ref, ts in rows if ref and (stamp := rules.ts(ts))}


def deadline_for(part: dict | None, cutoff: dt.datetime | None) -> dt.datetime | None:
    """Use the last departure for a viable revision, otherwise the delivery deadline."""
    option = (part or {}).get("option")
    if option and str(part.get("default_route_id", "")).startswith("offer-"):
        delivery = option.get("deliveryState") or {}
        if delivery.get("conflict"):
            return cutoff
        if delivery.get("needs_revised_terms") and delivery.get("can_propose_revision"):
            # The revision only helps if the goods can still leave in time for the cutoff.
            return rules.ts((delivery.get("departure") or {}).get("deadline")
                            or delivery.get("depart_by") or option.get("departBy")) or cutoff
        if deliver_by := rules.ts(delivery.get("deliver_by")):
            return deliver_by
        return rules.ts(option.get("latestUsefulAt")) or cutoff
    return cutoff


def next_check(component_id: str, step: dict, reviews: dict[str, dt.datetime], now: dt.datetime,
               deadline: dt.datetime | None = None) -> dt.datetime | None:
    """When Dockside will next look at this item, or None if it is not waiting on anyone (or follow-ups are off)."""
    conf = settings()
    if not conf["enabled"] or not step or step.get("reviewRequired") or (step.get("state") not in WAITING and not step.get("unverified")):
        return None
    if deadline and now >= deadline:
        return None  # too late to change anything for launch
    marks = [stamp for stamp in (rules.ts(step.get("since")), reviews.get(component_id)) if stamp]
    if not marks:
        return now  # never looked at: look now
    last = max(marks)
    interval = dt.timedelta(hours=conf["after_hours"])
    if deadline and deadline > last:
        interval = min(interval, max(dt.timedelta(minutes=15), (deadline - last) / 2))  # closer deadline, shorter wait
    if step.get("urgent"):
        interval = min(interval, dt.timedelta(minutes=30))
    due = last + interval
    if deadline:
        due = min(due, max(last, deadline - dt.timedelta(hours=1)))
    return due


def when(moment: dt.datetime, now: dt.datetime) -> str:
    local = moment.astimezone(rules.timezone())
    return f"{local:%H:%M}" if local.date() == now.astimezone(rules.timezone()).date() else f"{local:%a %H:%M}"


def promise(component_id: str, step: dict, reviews: dict[str, dt.datetime], now: dt.datetime,
            deadline: dt.datetime | None = None) -> str:
    """The sentence that tells the reader what Dockside does next and when."""
    due = next_check(component_id, step, reviews, now, deadline)
    if not due:
        return ""
    if due <= now:
        return "Dockside is following up now."
    return f"Dockside follows up at {when(due, now)} if nothing changes."


def root_finding(part: dict) -> str:
    """The finding id the dashboard groups this part's work under, so follow-up actions show on the right card."""
    route = part.get("default_route_id") or ""
    if route.startswith(("offer-", "gap-")):
        return route
    if route.startswith("stock-"):
        return f"gap-{part['id']}"
    return f"ship-{route}"


def stalled(snapshot: dict, now: dt.datetime | None = None) -> list[dict]:
    now = now or rules.now()
    reviews = last_reviews()
    cutoff = rules.ts(snapshot.get("cutoff"))
    priority = snapshot.get("priorityId")
    constraints = set(next((e.get("constraintIds", []) for e in snapshot.get("editions", []) if e.get("id") == priority), []))
    out, seen = [], set()
    for part in snapshot["components"]:
        if part.get("includedIn"):
            continue  # the parent part carries the work
        step = part["next_step"]
        deadline = deadline_for(part, cutoff)
        due = next_check(part["id"], step, reviews, now, deadline)
        if not due or due > now or (step.get("action_id") and step["action_id"] in seen):
            continue
        seen.add(step.get("action_id"))
        route = next((r for r in part["routes"] if r["id"] == part["default_route_id"]), None)
        route_steps = (route or {}).get("steps", [])
        delivery = ((part.get("option") or {}).get("deliveryState") or {}) if route and route["id"].startswith("offer-") else {}
        revised = delivery.get("needs_revised_terms", False)
        task = step.get("task") or delivery.get("task")
        staff_questions = {}
        open_steps = [s for s in route_steps
                      if s["state"] not in {"confirmed", "superseded"} and not s.get("reminder_of")]
        for pending in [*open_steps, step]:
            qid = pending.get("message_id")
            if qid and pending.get("question") and pending.get("person") and pending.get("state") in {"waiting", "Waiting for reply", "Needs you"}:
                if task and qid != task["id"]:
                    continue
                staff_questions[qid] = {
                    "who": pending["person"], "what": pending["question"], "asked_at": pending.get("since"),
                }
                if task and qid == task["id"]:
                    staff_questions[qid]["id"] = qid
        out.append({"component_id": part["id"], "part": part["name"], "status": part["status"],
                    "for_collectors": any(a["editionId"] == priority for a in part.get("allocations", [])),
                    "usable": part["usable"], "required": part["required"],
                    "finding_id_for_new_actions": root_finding(part),
                    "route": route["name"] if route else None, "route_id": route["id"] if route else None,
                    "open_steps": [s["name"] + ": " + s["detail"] for s in open_steps],
                    "waiting_on": step.get("base_text", step["text"]), "state": step["state"], "since": step["since"],
                    "action_id": step["action_id"],
                    "act_by": deadline.isoformat(timespec="minutes") if deadline else None,
                    "arrival_window": release.amendment_window(delivery, cutoff) if revised else None,
                    "verified_eta": release.verified_eta(delivery),
                    "booking_ref": delivery.get("booking_ref"), "slot_window": delivery.get("slot_window"),
                    "vehicle_limit": delivery.get("vehicle_limit"), "needs_revised_terms": revised,
                    "departure": delivery.get("departure"),
                    "revised_reason": delivery.get("revised_reason"), "open_staff_questions": list(staff_questions.values()),
                    "task": task, "delivery_state": delivery or None})
    no_deadline = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    out.sort(key=lambda item: (item["component_id"] not in constraints, not item["for_collectors"],
                               item["status"] != "short", rules.ts(item["act_by"]) or no_deadline))
    return out[: settings()["max_per_cycle"]]


PROMPT = """These parts have had no progress for a while. Nothing may wait silently: for each one, take the next step yourself now.
It is {now}. Items:
{items}

How to decide, per part:
- First call get_action on action_id (if any) and check open actions in get_situation. Inspect any matching requests with get_action.
  Read open_staff_questions for who was asked, what they were asked and when, and the ID of any conflict task. Never repeat an identical open request, even if it has
  a different finding ID.
- Schedule each intervention by act_by, including the answer-by time and the approval due time. For a rush amendment, act_by is the last
  departure time. The amended deliver_by is an arrival time: it must be strictly after arrival_window.after, at or before
  arrival_window.latest, and at or after verified_eta. Use the full date, time and offset.
- Use departure as the gate's verdict. A verified reply gives the reported departure time. A verified carrier_position shows the truck
  had left by the observation time (departed_at). Cite evidence_id and source_label, and use existing verified evidence. If departure is
  passed, reopening requires evidence the truck left by departure.deadline, a current ETA and a confirmed slot.
- Rush delivery with delivery_state.conflict takes precedence. For an open task, remind owner_reconcile using task.id and the finding ID
  for this delivery. For an answered task, use task.answer and continue with the evidence still outstanding. Ask owner_logistics on a linked
  confirmation thread for a booking reference, eligible vehicle and verified ETA by cutoff. If already in transit and departure is still
  unverified, request actual departure.
  Use clears_when and cite the supplier message ID. Keep any existing request that already covers the check.
  Do not propose an amendment while the conflict is open. Skip the other rules for this delivery.
- Rush delivery with needs_revised_terms: if delivery_state.can_propose_revision is false, escalate revised_reason to the buyer and
  finish this item.
  Inspect delivery_state.action_id and its delivery_terms. If pending_revision_action_id exists, review that action and continue its
  open checks under its finding ID. Otherwise, with a verified_eta, propose ONE amendment through propose_action with kind 'order'
  and a typed commitment. Set revises_action_id to the original action ID, use its delivery_id, supplier recipient, commercial terms,
  conditions and vehicle_limit_t, and set deliver_by within the arrival window. Use the original cost_eur and category so the assigned
  approver can sign the exact terms. Use finding_id 'followup-<finding_id_for_new_actions>-{stamp}' for the amendment.
  Once the amendment is proposed, email an authorized receiving contact for a fresh unload slot covering verified_eta, under the
  amendment's finding ID. The earlier slot does not establish the revised slot. If verified_eta, booking_ref or required vehicle
  confirmation is missing, email a listed transport contact for the missing evidence and use any open request that already covers it.
  If the carrier has left and departure is still unverified, ask for the actual departure time and current ETA. Dockside checks departure evidence against act_by
  and requires a confirmed slot for that ETA once act_by has passed.
  Wait for a verified ETA before choosing arrival terms. Use kind 'confirm_terms', the same delivery_id and confirmation_kind 'dock_slot'
  or 'book_truck' for the emails. Skip the generic reminder and slot rules below for this delivery.
- Rush delivery with signed conditions still open: use delivery_state.contacts and the same delivery_id for the missing written checks.
  Ask about the specific missing evidence rather than repeating an unrelated reminder. Use confirmation_kind 'book_truck' or 'dock_slot'.
- A supplier has not answered: send ONE short reminder to the same contact in their language (kind 'ask', free, goes out at once),
  referring to the earlier message. Give a concrete answer-by time before act_by. If transport is the gap, ask for the carrier's
  booking reference and ETA.
- A delivery will need unloading in {site_name} and no unload slot action exists yet: propose kind 'dock_slot' for the expected arrival window,
  as a Discord message to {site_lead} ({site_lead_role}). Apply the configured approval and stop-window policy.
- A pickup is overdue: ask the supplier for the pickup time, the carrier's booking reference and ETA.
- The supplier reported the goods on the way but nothing confirms it (state 'On the way', waiting_on mentions the tracker): ask the supplier
  for the carrier's booking reference, vehicle and current ETA to {site_name}, so receiving can plan for it.
- Only a person can do it (state 'Needs you', e.g. a phone call): escalate on Discord to the owner named in the step, or the Head of
  Procurement if none, saying exactly what to do, why it matters and by when (kind 'escalate').
- If nothing useful can be done before act_by, say so and do nothing.
For reminders and escalations use finding_id 'followup-<finding_id_for_new_actions>-{stamp}'. For a dock slot use finding_id_for_new_actions
itself. Plain words, no part codes in 'what' and 'why'. Finish with one line per part: what you did."""


def run(snapshot: dict, on_step=None) -> str | None:
    from core import agent, llm_health
    if not llm_health.available():
        return None  # an outage must not count as a review
    items = stalled(snapshot)
    if not items:
        return None
    for item in items:  # log first: a failing model call must not cause a review storm
        actions.log_event(REVIEW_KIND, item["component_id"], f"follow-up on {item['part']}: {item['waiting_on']}")
    now = rules.now()
    site_lead_role = rules.cfg("site_lead_role")
    prompt = PROMPT.format(now=f"{now:%a %d %b %H:%M}", items=json.dumps(items, ensure_ascii=False, indent=1),
                           stamp=now.strftime("%Y%m%d%H%M"), site_name=rules.cfg("site_name"),
                           site_lead=rules.cfg("people").get(site_lead_role) or site_lead_role, site_lead_role=site_lead_role)
    return agent.run(prompt, on_step, effort="medium")
