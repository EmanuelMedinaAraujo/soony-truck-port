"""Dockside server: background loop (sync -> rebuild -> read new docs -> rules -> agent -> follow-ups) + JSON API + web UI.

Run:  .venv/bin/uvicorn server.app:app --port 8000
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sync"))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import soony_sync  # noqa: E402
from core import actions, agent, ingest, rules  # noqa: E402

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "90"))
AUTO_AGENT = os.environ.get("AUTO_AGENT", "1") == "1"
app = FastAPI(title="Dockside")
STATE = {"last_sync": None, "last_change": None, "sync_error": None, "busy": None, "agent_summary": None}
LOCK = threading.Lock()
listeners: list = []  # callables(event_dict) — the Discord bot registers here


def notify(kind: str, payload: dict) -> None:
    for fn in list(listeners):
        try:
            fn({"kind": kind, **payload})
        except Exception:
            traceback.print_exc()


def cycle(force_plan: bool = False, plan_model: str | None = None) -> dict:
    """One pass of the whole pipeline. Safe to call from the loop or an API request."""
    with LOCK:
        out = {"changes": 0}
        token = os.environ.get("SOONY_TOKEN")
        try:
            STATE["busy"] = "sync"
            events = soony_sync.sync_once(token) if token else []
            STATE["last_sync"], STATE["sync_error"] = rules.now().isoformat(timespec="seconds"), None
            out["changes"] = len(events)
        except Exception as e:
            STATE["sync_error"] = f"{type(e).__name__}: {e}"
            events = []
        if events:
            STATE["last_change"] = rules.now().isoformat(timespec="seconds")
            paths = [e["path"] for e in events]
            actions.log_event("data_drop", None, f"{len(events)} file(s) changed: " + ", ".join(paths[:6]) + (" …" if len(paths) > 6 else ""))
            for e in events:
                for rc in (e.get("row_changes") or [])[:20]:
                    actions.log_event("row_change", rc.get("key"), f"{e['path'].split('/')[-1]}: {rc['kind']} {json.dumps(rc.get('fields') or {}, ensure_ascii=False)[:200]}")
            notify("data_drop", {"paths": paths})
        if events or force_plan or not rules.DB_PATH.exists():
            STATE["busy"] = "rebuild"
            ingest.build()
            if os.environ.get("OPENAI_API_KEY"):
                STATE["busy"] = "reading documents"
                from core import extract_docs
                stats = extract_docs.run()
                new = [k for k, v in stats.items() if v != "cached"]
                if new:
                    actions.log_event("docs_read", None, f"read {len(new)} new document(s)")
                    ingest.build()
            if AUTO_AGENT and os.environ.get("OPENAI_API_KEY"):
                STATE["busy"] = "agent planning"
                before = {a["id"] for a in actions.list_actions()}
                steps = {"n": 0}
                labels = {"get_situation": "reading the launch picture", "get_shipment": "checking a shipment", "query_db": "querying the data",
                          "search_documents": "reading documents", "plan_truck": "planning a truck", "what_if": "running a what-if",
                          "propose_action": "writing an action"}

                def progress(name, args, out):
                    steps["n"] += 1
                    STATE["busy"] = f"agent planning: step {steps['n']}, {labels.get(name, name)}"
                    if name == "propose_action":
                        a = actions.get(out.get("created") or out.get("done_automatically") or "")
                        if a:  # stream each action to the UI/Discord as soon as it exists
                            notify({"auto": "action_auto", "scheduled": "action_scheduled"}.get(a["status"], "action_proposed"), {"action": a})

                STATE["agent_summary"] = agent.plan(progress, model=plan_model)
                actions.log_event("agent_plan_done", None, f"planned in {steps['n']} steps with {plan_model or agent.PLAN_MODEL}")
        if os.environ.get("IMAP_HOST") and os.environ.get("OPENAI_API_KEY"):
            try:
                STATE["busy"] = "reading supplier replies"
                from core import inbox
                for r in inbox.fetch():
                    notify("reply", {"reply": r})
                if inbox.unhandled():
                    STATE["busy"] = "agent handling replies"
                    n = {"k": 0}

                    def rprog(name, args, out):
                        n["k"] += 1
                        STATE["busy"] = f"agent handling replies: step {n['k']}"
                        if name == "propose_action":
                            a = actions.get(out.get("created") or out.get("done_automatically") or out.get("scheduled") or "")
                            if a:
                                notify({"auto": "action_auto", "scheduled": "action_scheduled"}.get(a["status"], "action_proposed"), {"action": a})
                    STATE["reply_summary"] = agent.handle_replies(rprog)
                    actions.log_event("agent_replies_done", None, (STATE["reply_summary"] or "")[:300])
            except Exception as e:
                actions.log_event("inbox_error", None, f"{type(e).__name__}: {e}")
        STATE["busy"] = "executing scheduled actions"
        for a in actions.due_scheduled():
            for m in a["messages"]:
                if m["status"] == "draft":
                    from core import outbound
                    outbound.send(m)
            a = actions.set_status(a["id"], "sent", f"executed by Dockside: nobody stopped it within the window ({a['approver_name']} was informed)")
            notify("action_executed", {"action": a})
        STATE["busy"] = "follow-ups"
        for a in actions.due_followups():
            if a["status"] in ("proposed", "escalated"):
                a = actions.escalate(a)
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
    _m = _sq.connect(rules.DB_PATH)
    descs = dict(_m.execute("SELECT component_id, description FROM components").fetchall())
    for s in relevant:
        s["material_names"] = [rules.plain(descs.get(m), m) for m in s["materials"]]
    bind_id = next((c["component"] for c in sorted(r["coverage"].values(), key=lambda c: c["alloc"].get(prio, 0) - c["need"].get(prio, 0))
                    if prio in c["need"] and not c["suspect_master_data"] and c["need"][prio] > c["alloc"][prio]), None)
    with actions.con() as c:
        ev = [dict(x) for x in c.execute("SELECT * FROM events WHERE kind<>'row_change' ORDER BY ts DESC LIMIT 40")]
    import sqlite3
    m = sqlite3.connect(rules.DB_PATH)
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
        "system": {**STATE, "files": n_files, "conflicts": n_conf, "docs_read": n_docs, "poll_seconds": POLL_SECONDS},
    }


@app.get("/api/state")
def state():
    return _compact_state()


class Decision(BaseModel):
    decision: str
    by: str = "web"
    note: str = ""


def decide_and_dispatch(aid: str, decision: str, by: str, note: str = "") -> dict:
    """The one approval path for web and Discord: record the decision, release messages, tell everyone."""
    a = actions.decide(aid, decision, by, note)
    if a["status"] == "approved":
        dispatch(a)
    a = actions.get(aid)
    notify("action_decided", {"action": a})
    return a


@app.post("/api/actions/{aid}/decide")
def decide(aid: str, d: Decision):
    try:
        return decide_and_dispatch(aid, d.decision, d.by, d.note)
    except KeyError:
        raise HTTPException(404, f"No decision {aid}. It may have been replaced; reload the page.")
    except ValueError as e:
        raise HTTPException(409, f"{e}. Someone already decided; reload to see who.")


def dispatch(a: dict) -> None:
    """Send what an approval releases. E-mail goes through the team's supplier mailer when configured, else the outbox."""
    from core import outbound
    for m in a["messages"]:
        if m["status"] == "draft":
            outbound.send(m)
    actions.set_status(a["id"], "sent", "messages released after approval", next_check_at=rules.now().isoformat(timespec="seconds"))


class Ask(BaseModel):
    q: str


@app.post("/api/chat")
def chat(a: Ask):
    steps = []
    ans = agent.chat(a.q, lambda n, args, out: steps.append({"tool": n, "args": args}))
    actions.log_event("chat", None, a.q[:200])
    return {"answer": ans, "steps": steps}


@app.post("/api/cycle")
def run_cycle(plan: bool = False, model: str | None = None):
    """Start a pipeline pass in the background; progress shows in /api/state system.busy."""
    if LOCK.locked():
        return {"started": False, "reason": f"already running: {STATE['busy']}"}
    threading.Thread(target=cycle, kwargs={"force_plan": plan, "plan_model": model}, daemon=True).start()
    return {"started": True}


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/v/{name}")
def variant(name: str):
    """UI prototypes: web/variants/<name>.html, same API. /v/ lists them."""
    import re as _re
    f = ROOT / "web" / "variants" / f"{name}.html"
    if not _re.fullmatch(r"[a-z0-9-]+", name) or not f.exists():
        raise HTTPException(404, "No such prototype. See /v/ for the list.")
    return FileResponse(f)


@app.get("/v/")
def variants():
    from fastapi.responses import HTMLResponse
    names = sorted(p.stem for p in (ROOT / "web" / "variants").glob("*.html"))
    links = "".join(f'<li><a href="/v/{n}">{n}</a></li>' for n in names)
    return HTMLResponse(f'<!doctype html><meta charset=utf-8><title>Dockside prototypes</title><body style="font:16px system-ui;padding:24px;background:#fff">'
                        f'<h1>Dockside prototypes</h1><ul><li><a href="/">current (main)</a></li>{links}</ul></body>')
