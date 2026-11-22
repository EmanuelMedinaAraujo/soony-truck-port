"""Dockside server: background loop (sync -> rebuild -> read new docs -> rules -> agent -> follow-ups) + JSON API + web UI.

Run: .venv/bin/uvicorn server.app:app --env-file .env --port 8765
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from contextlib import closing
from html import escape
from ipaddress import ip_address, ip_network
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sync"))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import soony_sync  # noqa: E402
from core import actions, agent, approvals, chase, dispatch, ingest, outbound, release, rules, voice  # noqa: E402

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "90"))
AUTO_AGENT = os.environ.get("AUTO_AGENT", "1") == "1"
app = FastAPI(title="Dockside")


class ApprovalAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (isinstance(args, tuple) and len(args) >= 3 and str(args[2]).startswith(approvals.PATH))


logging.getLogger("uvicorn.access").addFilter(ApprovalAccessFilter())


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Browsers must re-check page files on every load, so UI updates show up without a hard refresh."""
    response = await call_next(request)
    if request.method == "GET" and not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response
STATE = {"last_sync": None, "last_change": None, "sync_error": None, "last_inbox": None, "inbox_error": None,
         "busy": None, "agent_summary": None}
LOCK = threading.Lock()
listeners: list = []  # callables(event_dict) — the Discord bot registers here


def notify(kind: str, payload: dict) -> None:
    for fn in list(listeners):
        try:
            fn({"kind": kind, **payload})
        except Exception:
            traceback.print_exc()


def notify_action(result: dict) -> None:
    aid = result.get("created") or result.get("done_automatically") or result.get("scheduled")
    if aid:
        approvals.email(aid)
        a = actions.get(aid)
        kind = "action_held" if a["held_reason"] else {
            "auto": "action_auto", "scheduled": "action_scheduled"}.get(a["status"], "action_proposed")
        notify(kind, {"action": a})


def llm_paused(e) -> None:
    """The OpenAI key failed: remember to re-plan later, tell people once, keep everything else running."""
    from core import llm_health
    STATE["llm_pending"] = True
    st = llm_health.status()
    if st.get("new", True):
        actions.log_event("llm_paused", None, str(e))
        notify("llm_down", {"message": str(e), "kind": getattr(e, "kind", "")})
        try:
            import json as _j
            st["new"] = False
            llm_health.STATUS.write_text(_j.dumps(st, indent=1))
        except Exception:
            pass


def refresh_evidence(force_rebuild: bool = False) -> list[dict]:
    """Publish evidence checkpoints only after the matching data is readable."""
    from core import extract_docs, inbox, llm_health

    with outbound.DISPATCH_LOCK:
        events = []
        token = os.environ.get("SOONY_TOKEN")
        try:
            if token:
                events = soony_sync.sync_once(token)
            STATE["files_pending"] = STATE.get("files_pending", True) or bool(events)
            if STATE["files_pending"] or force_rebuild or not rules.DB_PATH.is_file():
                STATE["files_pending"] = True
                release.record_check("files", valid=False)
                if os.environ.get("OPENAI_API_KEY"):
                    stats = extract_docs.run()
                    if any(value.startswith(("error", "empty", "paused")) for value in stats.values()):
                        raise ValueError("Document evidence could not be read")
                elif token and not all(extract_docs._cached(path) for path in extract_docs.documents()):
                    raise ValueError("New documents need extraction before paid release")
                ingest.build()
                STATE["files_pending"] = False
            if token:
                release.record_check("files")
                STATE["last_sync"], STATE["sync_error"] = rules.now().isoformat(timespec="seconds"), None
        except llm_health.LLMUnavailable as exc:
            release.record_check("files", valid=False)
            llm_paused(exc)
        except Exception as exc:
            release.record_check("files", valid=False)
            STATE["files_pending"] = True
            STATE["sync_error"] = type(exc).__name__
            actions.log_event("sync_error", None, type(exc).__name__)
        if events:
            STATE["llm_pending"] = True
            STATE["last_change"] = rules.now().isoformat(timespec="seconds")
            paths = [e["path"] for e in events]
            actions.log_event("data_drop", None, f"{len(events)} file(s) changed: " + ", ".join(paths[:6]) + (" …" if len(paths) > 6 else ""))
            for e in events:
                for rc in (e.get("row_changes") or [])[:20]:
                    actions.log_event("row_change", rc.get("key"), f"{e['path'].split('/')[-1]}: {rc['kind']} {json.dumps(rc.get('fields') or {}, ensure_ascii=False)[:200]}")
            notify("data_drop", {"paths": paths})
        if os.environ.get("IMAP_HOST"):
            try:
                for reply in inbox.fetch():
                    notify("reply", {"reply": reply})
                release.record_check("mailbox")
                STATE["last_inbox"], STATE["inbox_error"] = rules.now().isoformat(timespec="seconds"), None
            except Exception as exc:
                release.record_check("mailbox", valid=False)
                STATE["inbox_error"] = type(exc).__name__
                actions.log_event("inbox_error", None, type(exc).__name__)
        return events


def cycle(force_plan: bool = False, plan_model: str | None = None) -> dict:
    """One pass of the whole pipeline. Safe to call from the loop or an API request."""
    with LOCK:
        STATE["busy"] = "refreshing file and supplier evidence"
        events = refresh_evidence(force_rebuild=force_plan)
        out = {"changes": len(events)}
        STATE["busy"] = "checking spending approvals"
        for a in actions.release_free():
            notify_action({"done_automatically": a["id"]})
        approvals.email_open()
        STATE["busy"] = "executing scheduled actions"
        for a in actions.due_scheduled():
            a = outbound.dispatch(a["id"], "executed by Dockside after the stop window")
            if a["status"] == "sent":
                notify("action_executed", {"action": a})
        for aid in actions.pending_dispatch():
            outbound.dispatch(aid)
        from core import llm_health
        if not STATE.get("files_pending") and (events or force_plan or
                (STATE.get("llm_pending") and llm_health.available())):
            try:
                STATE["llm_pending"] = False
                if AUTO_AGENT and os.environ.get("OPENAI_API_KEY"):
                    STATE["busy"] = "agent planning"
                    steps = {"n": 0}
                    labels = {"get_situation": "reading the launch picture", "get_shipment": "checking a shipment", "query_db": "querying the data",
                              "search_documents": "reading documents", "plan_truck": "planning a truck", "what_if": "running a what-if",
                              "propose_action": "writing an action"}

                    def progress(name, args, out):
                        steps["n"] += 1
                        STATE["busy"] = f"planning: {labels.get(name, name)}"
                        if name == "propose_action":
                            notify_action(out)

                    STATE["agent_summary"] = agent.plan(progress, model=plan_model)
                    actions.log_event("agent_plan_done", None, f"planned in {steps['n']} steps with {plan_model or agent.PLAN_MODEL}")
            except llm_health.LLMUnavailable as e:
                llm_paused(e)
        STATE["busy"] = "chasing supplier confirmations"
        try:
            chase_result = chase.run()
            for action_id in chase_result.pop("actions"):
                notify_action({"done_automatically": action_id})
            out["chase"] = chase_result
            STATE["chase_error"] = None
        except Exception as e:
            STATE["chase_error"] = type(e).__name__
            actions.log_event("chase_error", None, type(e).__name__)
        STATE["busy"] = "checking voice calls"
        for check in (voice.prepare_open, voice.collect):
            try:
                for r in check() or ():
                    notify("reply", {"reply": r})
            except Exception as e:
                actions.log_event("voice_error", None, f"{check.__name__}: {type(e).__name__}")
        if AUTO_AGENT and os.environ.get("OPENAI_API_KEY"):
            try:
                from core import inbox
                if inbox.unhandled():
                    STATE["busy"] = "agent handling replies"
                    n = {"k": 0}

                    def rprog(name, args, out):
                        n["k"] += 1
                        STATE["busy"] = "reading supplier replies"
                        if name == "propose_action":
                            notify_action(out)
                    STATE["reply_summary"] = agent.handle_replies(rprog)
                    actions.log_event("agent_replies_done", None, (STATE["reply_summary"] or "")[:300])
            except llm_health.LLMUnavailable as e:
                llm_paused(e)
            except Exception as e:
                actions.log_event("reply_handling_error", None, type(e).__name__)
        if AUTO_AGENT and os.environ.get("OPENAI_API_KEY"):
            try:  # nothing waits silently: remind, book the unload slot or escalate when an item has gone quiet
                from core import dispatch, followup
                snap = dispatch.snapshot(STATE.get("last_sync"))
                if followup.stalled(snap):
                    STATE["busy"] = "following up on items with no progress"
                    f = {"k": 0}

                    def fprog(name, args, out):
                        f["k"] += 1
                        STATE["busy"] = "following up on open items"
                        if name == "propose_action":
                            notify_action(out)
                    summary = followup.run(snap, fprog)
                    if summary:
                        actions.log_event("followup_done", None, summary[:300])
            except llm_health.LLMUnavailable as e:
                llm_paused(e)
            except Exception as e:
                actions.log_event("followup_error", None, f"{type(e).__name__}: {e}")
        STATE["busy"] = "follow-ups"
        for a in actions.due_followups():
            state = release.decision_state(a)
            if state["terminal"] == "Expired — no longer useful":
                actions.hold(a["id"], state["held_reason"], expire=True)
            elif state["can_approve"]:
                a = actions.escalate(a)
                if a:
                    notify("action_escalated", {"action": a})
        STATE["busy"] = None
        return out


def loop():
    while True:
        try:
            cycle()
        except Exception:
            traceback.print_exc()
            STATE["busy"] = None
        time.sleep(POLL_SECONDS)


@app.on_event("startup")
def _start():
    from server import discord_bot
    STATE["discord"] = bool(discord_bot.start(listeners, decide_and_dispatch))
    if os.environ.get("NO_LOOP") != "1":
        threading.Thread(target=loop, daemon=True).start()


# ------------------------------------------------------------------ API
def _compact_state() -> dict:
    r = rules.evaluate()
    prio = rules.cfg("priority_product")
    head = next(f for f in r["findings"] if f["id"] == "ce-at-risk")
    relevant = [s for s in r["inbound"] if s["planned_for_launch"] or s["arrival_at_dc"] is None or s["makes_cutoff"]]
    later = len(r["inbound"]) - len(relevant)
    import sqlite3 as _sq
    from contextlib import closing
    with closing(_sq.connect(rules.DB_PATH)) as _m:
        descs = dict(_m.execute("SELECT component_id, description FROM components").fetchall())
    for s in relevant:
        s["material_names"] = [rules.plain(descs.get(m), m) for m in s["materials"]]
    bind_id = next((c["component"] for c in sorted(r["coverage"].values(), key=lambda c: c["alloc"].get(prio, 0) - c["need"].get(prio, 0))
                    if prio in c["need"] and not c["suspect_master_data"] and c["need"][prio] > c["alloc"][prio]), None)
    with actions.tx() as c:
        ev = [dict(x) for x in c.execute("SELECT * FROM events WHERE kind<>'row_change' ORDER BY ts DESC LIMIT 40")]
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(rules.DB_PATH)) as m:
        n_conf = m.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0]
        n_files = m.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        n_docs = m.execute("SELECT COUNT(DISTINCT path) FROM doc_facts").fetchone()[0]
    return {
        "now": r["now"], "deadline": rules.cfg("deadline"), "cutoff_hours": rules.cfg("receiving_buffer_hours"),
        "priority": prio, "picture": r["picture"],
        "headline": {"ce_copies": head["ce_copies"], "eur": head["eur_at_risk"], "detail": head["detail"],
                     "refund_per_copy": rules.cfg("missed_copy_cost_eur")[prio],
                     "binding": {"component": bind_id, "name": rules.plain(descs.get(bind_id))} if bind_id else None},
        "coverage": [{k: c[k] for k in ("component", "description", "need", "need_total", "usable_now", "gap", "erp_stock", "suspect_master_data")} | {"adjustments": [{"kind": a["kind"], "cut": a["cut"], "detail": a["detail"]} for a in c["adjustments"] if a["kind"] != "revision_unverified"]}
                     for c in r["coverage"].values()],
        "inbound": relevant, "inbound_later": later,
        "findings": r["findings"],
        "actions": actions.list_actions(),
        "events": ev,
        "system": {**STATE, "files": n_files, "conflicts": n_conf, "docs_read": n_docs, "poll_seconds": POLL_SECONDS,
                   "llm": __import__("core.llm_health", fromlist=["status"]).status()},
    }


@app.get("/api/state")
def state():
    return _compact_state()


@app.get("/api/dispatch")
def dispatch_view():
    snapshot = dispatch.snapshot(STATE["last_sync"])
    decisions = {decision["id"]: decision for decision in snapshot["decisions"]}
    disputed = {}
    for part in snapshot.get("components", []):
        delivery = (part["option"] or {}).get("deliveryState") or {}
        if delivery.get("conflict"):
            disputed[delivery["action_id"]] = delivery
    with actions.tx() as c:
        ids = [row[0] for row in c.execute("""SELECT id FROM actions
            WHERE status IN ('auto','approved','scheduled','sent','superseded','expired')
            AND json_extract(payload,'$.held_reason') IS NOT NULL""")]
    for aid in list(dict.fromkeys([*decisions, *ids, *disputed])):
        a = actions.get(aid)
        if aid not in decisions:
            decisions[aid] = {key: a[key] for key in ("id", "title", "rationale", "status", "approver_name", "next_check_at", "due_at")}
        terms = release.terms_for(a)
        decision = release.decision_state(a)
        if delivery := disputed.get(aid):
            conflict = delivery["conflict"]
            decision.update(can_approve=False, held_reason=delivery["revised_reason"],
                            hold_owner=conflict["owner_reconcile"], hold_clears=conflict["clears_when"],
                            conflict=conflict, confirmation_evidence=delivery["confirmation_evidence"])
        if decision["can_approve"] and terms:
            decision["confirmation_evidence"] = release.preview_confirmations(a)
        instruction = None
        if terms and not release.proposal_blocker(a):
            _, instruction = release.render_instruction(terms, bool(a["payload"].get("repeat_commitment")))
        decisions[aid].update(status=a["status"], cost=a["cost_eur"], details=a["payload"].get("details"),
                              messages=[{key: m[key] for key in ("to_name", "subject", "body", "body_en")} for m in a["messages"]],
                              commitment_id=a["commitment_id"], commitment_digest=release.approval_digest(a),
                              instruction=instruction, signatures=a["signatures"], **decision)
    snapshot["decisions"] = list(decisions.values())
    return snapshot


@app.get("/api/replies/{message_id}")
def get_reply(message_id: str):
    if actions.DB.is_file():
        with closing(sqlite3.connect(f"{actions.DB.resolve().as_uri()}?mode=ro", uri=True)) as log:
            log.row_factory = sqlite3.Row
            if log.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='replies'").fetchone():
                row = log.execute("""SELECT subject, body, from_name AS "from", received_at
                                     FROM replies WHERE id=?""", (message_id,)).fetchone()
                if row:
                    return dict(row)
    raise HTTPException(404, "Reply message not found")


@app.get("/api/calls")
def list_calls(action_id: str | None = None):
    return voice.list_calls(action_id)


@app.get("/api/calls/{message_id}")
def get_call(message_id: str):
    try:
        return voice.get(message_id)
    except KeyError:
        raise HTTPException(404, "Call message not found")


@app.post("/api/calls/{message_id}/prepare")
def prepare_call(message_id: str):
    try:
        return voice.prepare(message_id)
    except KeyError:
        raise HTTPException(404, "Call message not found")


@app.get("/api/parts/{component_id}/graph")
def component_graph(component_id: str):
    from core.depgraph import part_graph

    from core.depgraph import annotate

    graph = part_graph(component_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="Part not found")
    snap = dispatch.snapshot(STATE["last_sync"])
    part = next((c for c in snap["components"] if c["id"] == component_id), None)
    if part and part.get("includedIn"):
        part = next((c for c in snap["components"] if c["id"] == part["includedIn"].get("componentId")), part)
    return annotate(graph, part)


class Answer(BaseModel):
    text: str


@app.post("/api/questions/{message_id}/answer")
def answer_question(message_id: str, answer: Answer, request: Request):
    """A staff member answers Dockside's current question or task; the agent handles it like any reply."""
    from core import questions
    verified = verified_person(request)
    if not verified:
        raise HTTPException(403, "Sign in through the company network to answer.")
    who, source = verified
    question = questions.find(message_id)
    if not question:
        raise HTTPException(404, "That question no longer exists; reload the page.")
    if question.get("superseded"):
        raise HTTPException(409, "This question was replaced after the supplier's newer reply; answer the current task instead.")
    asked = question["person"]
    delegates = release.setting("delegates", {}).get(asked, [])
    if source == "local demo":
        who = f"{asked} (entered by the local demo operator)"
    elif who != asked and who not in delegates:
        raise HTTPException(403, f"This question is for {asked}. Only {asked} or a named delegate can answer it.")
    text = answer.text.strip()
    if not text:
        raise HTTPException(422, "Write an answer first.")
    try:
        reply = questions.answer(question, who, text[:4000])
    except KeyError:
        raise HTTPException(404, "That question no longer exists; reload the page.")
    notify("staff_answer", {"reply": reply})
    return {"ok": True, "reply_id": reply["id"]}


@app.get("/api/me")
def me(request: Request):
    """Who the dashboard is talking to, so it shows answer and approval controls only to the right person."""
    verified = verified_person(request)
    return {"person": verified[0] if verified else None, "source": verified[1] if verified else None}


class Decision(BaseModel):
    decision: str
    by: str = "web"
    note: str = ""
    commitment_digest: str | None = None


def decide_and_dispatch(aid: str, decision: str, by: str, note: str = "", *, source: str,
                        expires_at: str | None = None, commitment_digest: str | None = None) -> dict:
    """Record the decision and send through the outbound release gate."""
    with outbound.DISPATCH_LOCK:
        a = actions.decide(aid, decision, by, note, source=source, expires_at=expires_at,
                           commitment_digest=commitment_digest)
        if a["status"] == "approved":
            if release.applies(a):
                if os.environ.get("SOONY_TOKEN") or os.environ.get("IMAP_HOST"):
                    refresh_evidence()
            a = outbound.dispatch(aid)
    notify("action_decided", {"action": a})
    return a


def verified_person(request: Request) -> tuple[str, str] | None:
    """Resolve the login from the local Tailscale Serve reverse proxy."""
    host = request.client.host if request.client else ""
    local = host in {"127.0.0.1", "::1"}
    if login := request.headers.get("Tailscale-User-Login"):
        # Uvicorn can replace the local proxy peer with its forwarded tailnet IP.
        try:
            proxied = (request.headers.get("X-Forwarded-For") and
                       any(ip_address(host) in ip_network(network) for network in ("100.64.0.0/10", "fd7a:115c:a1e0::/48")))
        except ValueError:
            proxied = False
        if not (local or proxied):
            return None
        accounts = release.setting("staff_accounts", {})
        person = next((name for account, name in accounts.items() if account.casefold() == login.casefold()), None)
        return (person, "dashboard") if person else None
    if local and not any(request.headers.get(header) for header in ("X-Forwarded-For", "Forwarded", "Tailscale-User-Name")):
        if operator := release.setting("demo_local_operator", None):
            return operator, "local demo"
    return None


@app.post("/api/actions/{aid}/decide")
def decide(aid: str, d: Decision, request: Request):
    who = verified_person(request)
    if not who:
        raise HTTPException(403, "Sign in through the company network to decide, or use the link in the approval email.")
    try:
        return decide_and_dispatch(aid, d.decision, who[0], d.note, source=who[1], commitment_digest=d.commitment_digest)
    except actions.UnverifiedApprover as e:
        raise HTTPException(403, str(e))
    except KeyError:
        raise HTTPException(404, f"No decision {aid}. It may have been replaced; reload the page.")
    except ValueError as e:
        raise HTTPException(409, f"{e}. Someone already decided; reload to see who.")


def _approval_page(title: str, detail: str, button: str = "", status: int = 200, *, instruction: str = "") -> HTMLResponse:
    form = f'<form method="post"><button type="submit">{escape(button)}</button></form>' if button else ""
    return HTMLResponse(
        f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f'<title>{escape(title)} · Dockside</title><main><h1>{escape(title)}</h1><p>{escape(detail)}</p>'
        + (f'<pre>{escape(instruction)}</pre>' if instruction else "") + f'{form}</main></html>',
        status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                     "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'"},
    )


@app.api_route(approvals.PATH + "{token}/{decision}", methods=["GET", "POST"], include_in_schema=False)
def email_decision(token: str, decision: str, request: Request):
    if decision not in {"approve", "reject"}:
        return _approval_page("Unknown approval link", "Check the link in the original email.", status=404)
    try:
        link, state = approvals.lookup(token)
    except KeyError:
        return _approval_page("Unknown approval link", "Check the link in the original email.", status=404)
    if state == "used":
        return _approval_page("Decision already recorded", f"{link['title']} has status {link['status']}.")
    if state == "expired":
        return _approval_page("Approval link expired", "Open Dockside to check the current action.", status=410)
    label = "Approve now" if decision == "approve" else "Stop"
    if request.method == "GET":
        instruction = release.render_instruction(link["terms"])[1] if link["terms"] else ""
        return _approval_page(f"{label}?", f"{link['title']} (€{link['cost_eur']:,.2f})", label, instruction=instruction)
    try:
        action = decide_and_dispatch(link["action_id"], decision, link["approver_name"], source="email",
                                     expires_at=link["expires_at"], commitment_digest=link["commitment_digest"])
    except (ValueError, actions.UnverifiedApprover):
        return _approval_page("Decision unavailable", "This action was already decided or the link expired.", status=409)
    if action["held_reason"]:
        state = release.decision_state(action)
        return _approval_page(state["terminal"] or "Release held",
                              " ".join(filter(None, (state["held_reason"], state["hold_owner"], state["hold_clears"]))))
    if action["status"] in approvals.WAITING:
        pending = [name for name in actions.signers(action) if name not in action["signatures"]]
        return _approval_page("Signature recorded", f"Waiting for {', '.join(pending)}.")
    return _approval_page("Decision recorded", f"{link['title']} was {'approved' if decision == 'approve' else 'stopped'}.")


class Ask(BaseModel):
    q: str


@app.post("/api/chat")
def chat(a: Ask):
    steps = []
    from core import llm_health
    try:
        ans = agent.chat(a.q, lambda n, args, out: steps.append({"tool": n, "args": args}))
    except llm_health.LLMUnavailable as e:
        return JSONResponse({"answer": str(e), "steps": [], "error": e.kind}, status_code=503)
    actions.log_event("chat", None, a.q[:200])
    return {"answer": ans, "steps": steps}


@app.post("/api/cycle")
def run_cycle(plan: bool = False, model: str | None = None):
    """Start a pipeline pass in the background; progress shows in /api/state system.busy."""
    if LOCK.locked():
        return {"started": False, "reason": f"already running: {STATE['busy']}"}
    threading.Thread(target=cycle, kwargs={"force_plan": plan, "plan_model": model}, daemon=True).start()
    return {"started": True}


LAYOUTS = {"assembly": "Supply chain", "board": "Departure board", "map": "Dependency map",
           "papers": "Dispatch papers", "radio": "Dispatch signal"}


def layout_links(current: str | None = None) -> str:
    links = []
    paths = {"assembly": "/"}
    for name, label in LAYOUTS.items():
        href = paths.get(name, f"/v/{name}")
        if name in paths or (ROOT / "web" / "variants" / f"{name}.html").is_file():
            selected = ' aria-current="page"' if name == current else ""
            links.append(f'<li><a href="{href}"{selected}>{label}</a></li>')
    return "".join(links)


def render_layout(path: Path, name: str) -> HTMLResponse:
    label = LAYOUTS.get(name, name)
    menu = (f'<nav class="layout-picker" aria-label="Dockside layouts"><details>'
            f'<summary>Layout <strong>{label}</strong></summary>'
            f'<ul>{layout_links(name)}</ul></details></nav>')
    html = path.read_text(encoding="utf-8")
    html = html.replace("</head>", '<link rel="stylesheet" href="/layouts.css"></head>', 1)
    html = html.replace("<body>", f"<body>{menu}", 1)
    return HTMLResponse(html)


@app.get("/layouts.css")
def layout_styles():
    return FileResponse(ROOT / "web" / "layouts.css", media_type="text/css")


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "assembly" / "index.html")


app.mount("/assembly", StaticFiles(directory=ROOT / "web" / "assembly"), name="assembly_assets")


@app.get("/control-room")
def control_room():
    return RedirectResponse("/#operations")


@app.get("/v/{name}")
def variant(name: str):
    """UI prototypes: web/variants/<name>.html, same API. /v/ lists them."""
    f = ROOT / "web" / "variants" / f"{name}.html"
    if not re.fullmatch(r"[a-z0-9-]+", name) or not f.is_file():
        raise HTTPException(404, "No such prototype. See /v/ for the list.")
    return render_layout(f, name)


@app.get("/v/")
def variants():
    return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width, initial-scale=1">'
                        '<title>Dockside layouts</title><body style="color-scheme:light dark;font:16px/2 system-ui;padding:24px">'
                        f'<main><h1>Compare Dockside layouts</h1><ul>{layout_links()}</ul></main></body></html>')


@app.get("/api/tracking")
def tracking_view():
    from core import tracking

    return tracking.snapshot()


@app.get("/api/chaser")
def chaser_view(include_messages: bool = True):
    from core import chaser

    cases, status = chaser.cases(include_messages=include_messages)
    return {"cases": cases, "status": status}


@app.get("/api/cases")
def list_cases():
    from core import chaser

    return {"cases": chaser.list_native()}


@app.get("/api/cases/{case_id}")
def case_detail(case_id: str):
    from core import chaser

    case = chaser.detail(case_id)
    if case is None:
        raise HTTPException(404, "Case not found")
    return case
