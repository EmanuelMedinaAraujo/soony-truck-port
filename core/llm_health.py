"""LLM key health: classify OpenAI failures once, pause LLM work, and tell people in plain words.

The deterministic parts (sync, rules, map, dependency graph, scheduled actions) keep running without the LLM.
State lives in data/llm_status.json so the server, the UI and Discord all see the same thing.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

STATUS = Path(__file__).resolve().parent.parent / "data" / "llm_status.json"
RETRY_MINUTES = {"rate_limited": 2, "offline": 2, "out_of_credit": 15, "key_invalid": 15}
MESSAGES = {
    "out_of_credit": "The OpenAI key has run out of credit. Dockside keeps tracking and executing scheduled actions, but cannot read new documents, replies or answer questions. Add a new key as OPENAI_API_KEY in .env.",
    "key_invalid": "The OpenAI key was rejected. Add a valid key as OPENAI_API_KEY in .env.",
    "rate_limited": "OpenAI is rate limiting Dockside. It retries automatically in a few minutes.",
    "offline": "OpenAI cannot be reached. Dockside retries automatically.",
}


class LLMUnavailable(RuntimeError):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(MESSAGES.get(kind, kind))
        self.kind, self.detail = kind, detail


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone()


def _key_fingerprint() -> str:
    k = os.environ.get("OPENAI_API_KEY", "")
    return k[-6:] if k else ""


def status() -> dict:
    try:
        s = json.loads(STATUS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ok": True}
    if not s.get("ok") and s.get("key") != _key_fingerprint():
        return {"ok": True, "note": "new key detected"}  # someone replaced the key: try again immediately
    return s


def classify(exc: Exception) -> str | None:
    import openai
    if isinstance(exc, openai.AuthenticationError) or isinstance(exc, openai.PermissionDeniedError):
        return "key_invalid"
    if isinstance(exc, openai.RateLimitError):
        body = str(getattr(exc, "body", "") or exc).lower()
        return "out_of_credit" if ("insufficient_quota" in body or "quota" in body or "billing" in body) else "rate_limited"
    if isinstance(exc, openai.APIConnectionError) or isinstance(exc, openai.APITimeoutError):
        return "offline"
    return None


def record_failure(exc: Exception) -> LLMUnavailable | None:
    kind = classify(exc)
    if not kind:
        return None
    prev = status()
    s = {"ok": False, "kind": kind, "message": MESSAGES[kind], "detail": str(exc)[:300], "key": _key_fingerprint(),
         "since": prev.get("since") if not prev.get("ok") and prev.get("kind") == kind else _now().isoformat(timespec="seconds"),
         "retry_after": (_now() + dt.timedelta(minutes=RETRY_MINUTES[kind])).isoformat(timespec="seconds"),
         "new": prev.get("ok", True) or prev.get("kind") != kind}
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(s, indent=1))
    return LLMUnavailable(kind, str(exc)[:300])


def record_ok() -> None:
    if STATUS.exists():
        prev = status()
        STATUS.unlink(missing_ok=True)
        if not prev.get("ok"):
            from core import actions
            actions.log_event("llm_recovered", None, "OpenAI works again; Dockside resumes reading and planning")


def available() -> bool:
    """False while a failure is fresh; True again after the retry time or when the key changed."""
    s = status()
    if s.get("ok"):
        return True
    try:
        return _now() >= dt.datetime.fromisoformat(s["retry_after"])
    except (KeyError, ValueError):
        return True


def guard() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise LLMUnavailable("key_invalid", "OPENAI_API_KEY is not set")
    if not available():
        s = status()
        raise LLMUnavailable(s.get("kind", "offline"), s.get("detail", ""))
