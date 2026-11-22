"""Serve a local, offline Dockside demo against a disposable action log."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
from contextlib import closing
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import uvicorn
from dotenv import dotenv_values
from fastapi.responses import HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from core import actions, customer, ingest, llm_health, questions, release, rules
from examples import replay_gate
from examples.run_customer import apply_drop
from server.app import LOCK, app

SOURCE_LOG = ROOT / "data" / "actions.db"
KESTREL = ROOT / "examples" / "kestrel"


def copy_database(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(target)) as copy:
            original.backup(copy)


def sunday_replay() -> None:
    """Recreate the documented Sunday scenario using the release-gate order fixture."""
    policy = rules.CONFIG
    policy["demo_local_operator"] = {"value": "Paul Winter"}
    with rules.replay(policy, replay_gate.at("00:14")):
        # The challenge stock snapshot already contains the offer.
        with patch.object(replay_gate, "seed_offer"):
            replay_gate.seed_sent_order()
        supplier = release.sent_order_message(actions.get(replay_gate.ORDER))
    for clock, reply_id, body in [
        ("00:18", "stage-replay-ack", "We acknowledge the order and are checking the purchase-order reference."),
        ("02:07", "stage-replay-acceptance", replay_gate.ACCEPTANCE),
        ("04:17", "stage-replay-dispute", "We received no dispatch instruction."),
    ]:
        with rules.replay(policy, replay_gate.at(clock)):
            replay_gate.store_reply(supplier, reply_id, body)
    with rules.replay(policy, replay_gate.at("10:30")):
        questions.reconcile()


def kestrel_replay(scratch: Path) -> None:
    policy = customer.load(KESTREL / "business.json")
    rules.CONFIG.clear()
    rules.CONFIG.update(policy)
    os.environ["SOONY_NOW"] = rules.cfg("replay_at")
    rules.DB_PATH = scratch / "stock.db"
    current = scratch / "current"
    ingest.CURRENT = current
    sources = [KESTREL / "raw", KESTREL / "drops" / "2", KESTREL / "drops" / "3"]
    step = 0

    def import_drop(source: Path) -> list[str]:
        paths = apply_drop(source, current)
        ingest.build(rules.DB_PATH, current, facts_dir=KESTREL / "doc_facts")
        actions.log_event("data_drop", None, f"{len(paths)} files updated")
        return paths

    paths = import_drop(sources[0])

    @app.get("/demo/kestrel", response_class=HTMLResponse)
    def replay_page():
        summary = f"{len(paths)} source files" if step == 0 else ", ".join(Path(path).name for path in paths)
        disabled = " disabled" if step == len(sources) - 1 else ""
        return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kestrel file replay | Dockside</title>
<link rel="stylesheet" href="/assembly/assembly.css">
<style>
  body {{height:100dvh;display:grid;grid-template-rows:auto minmax(0,1fr)}}
  header {{display:flex;align-items:center;gap:.8rem;flex-wrap:wrap;padding:.55rem max(1rem,3vw);
    background:var(--sheet);border-bottom:1px solid var(--line)}}
  iframe {{width:100%;height:100%;border:0}}
  #drop-status {{color:var(--muted);font-size:.9rem}}
</style>
<header><strong>Kestrel file replay</strong>
<button id="next-drop" class="secondary-button"{disabled}>Load next file drop</button>
<span id="drop-status" role="status">Drop {step + 1} of {len(sources)} · {escape(summary)}</span></header>
<iframe src="/" title="Kestrel Toys dispatch board"></iframe>
<script>
const button = document.getElementById('next-drop');
const status = document.getElementById('drop-status');
button.onclick = async () => {{
  button.disabled = true;
  try {{
    const response = await fetch('/demo/kestrel/next', {{method: 'POST'}});
    if (!response.ok) throw new Error();
    const {{drop, files}} = await response.json();
    status.textContent = 'Drop ' + drop + ' of {len(sources)} · ' + files.join(', ');
    document.querySelector('iframe').contentWindow.location.reload();
    button.disabled = drop === {len(sources)};
  }} catch {{
    status.textContent = 'Couldn’t load the file drop. Try again.';
    button.disabled = false;
  }}
}};
</script>"""

    @app.post("/demo/kestrel/next")
    def next_drop():
        nonlocal paths, step
        with LOCK:
            if step == len(sources) - 1:
                return JSONResponse({"detail": "All file drops are loaded."}, status_code=409)
            paths = import_drop(sources[step + 1])
            step += 1
            return {"drop": step + 1, "files": [Path(path).name for path in paths]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("recorded", "sunday", "kestrel"), default="sunday")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--enable-questions", action="store_true", help="Allow the chat and microphone to use the OpenAI key")
    parser.add_argument("--include-recorded-activity", action="store_true", help="Keep the recorded supplier history in the Sunday walkthrough")
    args = parser.parse_args()

    if args.mode != "kestrel" and not rules.DB_PATH.is_file():
        parser.error(f"The challenge stock snapshot is missing at {rules.DB_PATH}")
    if (args.mode == "recorded" or args.include_recorded_activity) and not SOURCE_LOG.is_file():
        parser.error(f"The recorded action log is missing at {SOURCE_LOG}")

    if args.enable_questions:
        if not os.environ.get("OPENAI_API_KEY"):
            key = dotenv_values(ROOT / ".env").get("OPENAI_API_KEY")
            if not key:
                parser.error("Questions require OPENAI_API_KEY in the environment or .env.")
            os.environ["OPENAI_API_KEY"] = key
    else:
        os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("ELEVENLABS_API_KEY", None)
    clock = "2026-11-21T22:00:00+01:00" if args.mode == "recorded" else replay_gate.at("10:30").isoformat()
    os.environ["SOONY_NOW"] = clock

    @app.middleware("http")
    async def restrict_demo_changes(request, call_next):
        may_answer = (
            args.mode == "sunday"
            and request.method == "POST"
            and request.url.path.startswith("/api/questions/task:")
            and request.url.path.endswith("/answer")
        )
        may_ask = args.enable_questions and request.url.path in {"/api/chat", "/api/chat/transcribe"}
        may_load_drop = args.mode == "kestrel" and request.url.path == "/demo/kestrel/next"
        if request.method not in {"GET", "HEAD"} and not (may_answer or may_ask or may_load_drop):
            return JSONResponse({"detail": "This endpoint is disabled in the offline demo."}, status_code=405)
        return await call_next(request)

    with TemporaryDirectory(prefix="dockside-stage-") as directory:
        scratch = Path(directory)
        llm_health.STATUS = scratch / "llm_status.json"
        if args.mode != "kestrel":
            stock = scratch / "stock.db"
            copy_database(rules.DB_PATH, stock)
            rules.DB_PATH = stock
        actions.DB = scratch / "actions.db"
        if args.mode == "recorded" or args.include_recorded_activity:
            copy_database(SOURCE_LOG, actions.DB)
        actions.initialize()
        if args.mode == "sunday":
            sunday_replay()
        elif args.mode == "kestrel":
            kestrel_replay(scratch)
        uvicorn.run(app, host="127.0.0.1", port=args.port, lifespan="off")


if __name__ == "__main__":
    main()
