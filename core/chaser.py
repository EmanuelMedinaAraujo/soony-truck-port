"""Read-only client for Supplier Chaser's authenticated case API."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from dotenv import dotenv_values

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
TIMEOUT = 3.0
ACTIVE_STATUSES = {"open", "waiting", "answered", "escalated"}
STATES = {
    "NEW": "open", "INVESTIGATING": "open", "DRAFT_READY": "open", "NEEDS_APPROVAL": "open",
    "AWAITING_REPLY": "waiting", "RESPONSE_RECEIVED": "answered", "RECONCILING": "answered",
    "ESCALATED": "escalated", "BLOCKED": "escalated", "RESOLVED": "closed", "CANCELLED": "closed",
}


def _config() -> tuple[str, str]:
    # Uvicorn's reloader inherits the environment loaded when it first starts.
    settings = dotenv_values(ENV_FILE) if not (os.getenv("CHASER_URL") and os.getenv("CHASER_TOKEN")) else {}
    base = os.getenv("CHASER_URL") or settings.get("CHASER_URL") or ""
    token = os.getenv("CHASER_TOKEN") or settings.get("CHASER_TOKEN") or ""
    return base.rstrip("/"), token


def _get_json(client: httpx.Client, path: str):
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _messages(client: httpx.Client, case_id: str) -> tuple[str | None, str | None]:
    try:
        detail = _get_json(client, f"/api/v1/cases/{case_id}")
        messages = sorted(detail.get("messages", []), key=lambda message: message.get("at") or "", reverse=True)
        question = next((m.get("body") for m in messages if m.get("direction") == "outbound"
                         and m.get("status") not in {"superseded", "cancelled"}), None)
        reply = next((m for m in messages if m.get("direction") == "inbound"), {})
        summary = " ".join((reply.get("body") or reply.get("subject") or "").split())
        return question, (summary if len(summary) <= 300 else summary[:297].rstrip() + "...") or None
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return None, None


def cases(*, include_messages: bool = True, supplier_id: str | None = None,
          po_number: str | None = None, active_only: bool = False) -> tuple[list[dict], str]:
    """Return normalized cases and connection status. List failures leave Dockside usable."""
    base, token = _config()
    try:
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not token:
            return [], "chaser offline"
        with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT) as client:
            path = "/api/v1/cases"
            if supplier_id:
                path += "?supplier_id=" + quote(supplier_id, safe="")
            rows = _get_json(client, path)
            if not isinstance(rows, list):
                return [], "chaser offline"
            result = []
            for row in rows:
                po = row.get("po") or {}
                status = STATES.get(row["state"], "open")
                if (po_number and po.get("po_number") != po_number) or (active_only and status not in ACTIVE_STATUSES):
                    continue
                case_id = quote(row["id"], safe="")
                question, reply = _messages(client, case_id) if include_messages else (None, None)
                result.append({"case_id": row["id"], "supplier_id": row.get("supplier_id"), "supplier_name": row.get("supplier"),
                               "po_number": po.get("po_number"), "material": po.get("part_number"), "status": status,
                               "question": question, "last_reply_summary": reply, "updated_at": row.get("updated_at"),
                               "url": f"{base}/cases/{case_id}"})
        return result, "ok"
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError, AttributeError, TypeError):
        return [], "chaser offline"
