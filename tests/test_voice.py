import io
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException

from core import actions, inbox, rules, voice
from server.app import get_call, list_calls, prepare_call


@pytest.fixture
def call(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setitem(rules.CONFIG, "voice", {"value": {"enabled": True, "dial_out": False, "demo_number": None}})
    a = actions.propose("fixture", "Confirm game card pickup", "Keep the Hamburg launch on time", "ask",
                        cost_eur=0, eur_protected=0, ce_copies=0, category=None, owner=None,
                        payload={"details": "Confirm the existing slot only"},
                        messages=[{"channel": "phone_script", "to_name": "Shipping operations",
                                   "to_address": "+491234567890", "language": "de-DE",
                                   "subject": "Pickup", "body": "Ask for the time and booking reference."}])
    m = a["messages"][0]
    with actions.tx() as c:
        c.execute("UPDATE actions SET status='sent' WHERE id=?", (a["id"],))
        c.execute("UPDATE messages SET status='delivered' WHERE id=?", (m["id"],))
    return m


@pytest.fixture
def provider(monkeypatch):
    requests = []
    responses = {}

    def urlopen(req, timeout):
        assert req.get_header("Xi-api-key") == "test-key"
        url = urlsplit(req.full_url)
        assert url.hostname == "api.elevenlabs.io"
        path = url.path.removeprefix("/v1")
        requests.append((req.get_method(), path, parse_qs(url.query), json.loads(req.data) if req.data else None))
        result = responses[path]
        if isinstance(result, int):
            raise HTTPError(req.full_url, result, "mocked", None, io.BytesIO(b""))
        if callable(result):
            result = result(parse_qs(url.query))
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(voice, "urlopen", urlopen)
    responses.update({
        "/convai/agents": {"agents": []},
        "/voices": {"voices": [
            {"voice_id": "energetic", "labels": {"use_case": "advertisement"}},
            {"voice_id": "calm", "labels": {"description": "calm", "use_case": "conversational"}},
        ]},
        "/convai/agents/create": {"agent_id": "agent_test"},
    })
    return requests, responses


@pytest.fixture
def completed():
    return {
        "agent_id": "agent_test",
        "conversation_id": "conv_one",
        "status": "done",
        "metadata": {"start_time_unix_secs": 1795340000, "call_duration_secs": 60},
        "transcript": [
            {"role": "agent", "message": "Haben Sie die Abholzeit?"},
            {"role": "user", "message": "Zehn Uhr, falls das Tor frei ist."},
            {"role": "agent", "message": None},
        ],
        "analysis": {
            "transcript_summary": "Pickup is provisional.",
            "data_collection_results": {
                "confirmed": {"value": False},
                "pickup_or_delivery_time": {"value": "10:00 CET"},
                "open_issues": {"value": "Gate availability"},
            },
        },
    }


def test_prepare_and_api_are_idempotent(call, provider):
    requests, _ = provider
    mid = call["id"]
    assert get_call(mid)["status"] == "preparing"
    response = prepare_call(mid)
    assert response == prepare_call(mid) == {
        "message_id": mid, "to_name": call["to_name"], "subject": call["subject"],
        "script": call["body"], "status": "ready",
        "talk_url": "https://elevenlabs.io/app/talk-to?agent_id=agent_test", "result": None,
    }
    assert list_calls(call["action_id"]) == [response]
    assert list_calls("unknown") == []
    for endpoint in (get_call, prepare_call):
        with pytest.raises(HTTPException) as error:
            endpoint("unknown")
        assert error.value.status_code == 404
    assert [r[1] for r in requests] == ["/convai/agents", "/voices", "/convai/agents/create"]
    config = requests[-1][-1]
    agent = config["conversation_config"]["agent"]
    assert agent["language"] == "de"
    assert agent["first_message"].startswith("Guten Tag")
    assert "Confirm the existing slot only" in agent["prompt"]["prompt"]
    assert call["body"] in agent["prompt"]["prompt"]
    assert config["conversation_config"]["tts"]["voice_id"] == "calm"
    assert config["platform_settings"]["auth"]["enable_auth"] is False
    assert config["platform_settings"]["data_collection"]["confirmed"]["type"] == "boolean"
    message = actions.get(call["action_id"])["messages"][0]
    assert message["provider_id"] == "elevenlabs:agent_test"
    assert message["status"] == "delivered"


def test_prepare_retries_and_reuses_existing_remote_agent(call, provider):
    requests, responses = provider
    responses["/voices"] = 503
    assert voice.prepare(call["id"])["status"] == "unavailable"
    responses["/convai/agents"] = {"agents": [{"name": f"Dockside {call['id']}", "agent_id": "agent_existing"}]}
    assert voice.prepare(call["id"])["status"] == "ready"
    assert voice.talk_url(call["id"]).endswith("agent_id=agent_existing")
    assert [r[1] for r in requests] == ["/convai/agents", "/voices", "/convai/agents"]


def test_draft_or_disabled_never_contacts_provider(call, provider):
    requests, _ = provider
    with actions.tx() as c:
        c.execute("UPDATE messages SET status='draft' WHERE id=?", (call["id"],))
    assert voice.prepare(call["id"])["status"] == "unavailable"
    with actions.tx() as c:
        c.execute("UPDATE messages SET status='delivered' WHERE id=?", (call["id"],))
    rules.cfg("voice")["enabled"] = False
    assert voice.prepare(call["id"])["status"] == "unavailable"
    assert not requests


def test_collect_updates_progress_and_delivers_one_inbox_reply(call, provider, completed):
    requests, responses = provider
    mid = call["id"]
    voice.prepare_open()
    responses["/convai/conversations"] = {"conversations": [{"conversation_id": "conv_one", "status": "processing"}]}
    assert voice.collect() == []
    assert voice.get(mid)["status"] == "in_progress"
    assert inbox.unhandled() == []

    def page(query):
        if "cursor" in query:
            return {"conversations": [{"conversation_id": "conv_one", "status": "done"}]}
        return {"conversations": [{"conversation_id": "conv_empty", "status": "done", "user_message_count": 0}],
                "next_cursor": "older", "has_more": True}

    responses["/convai/conversations"] = page
    responses["/convai/conversations/conv_one"] = completed
    reply, = voice.collect()
    assert reply["message_id"] == "call:conv_one"
    assert reply["subject"] == "Call: Pickup"
    assert reply["msg_id"] == mid
    assert reply["action_id"] == call["action_id"]
    assert reply["from_name"] == call["to_name"]
    assert f"{call['to_name']}: Zehn Uhr" in reply["body"]
    assert '"confirmed": false' in reply["body"]
    pending, = inbox.unhandled()
    assert pending["id"] == reply["id"]
    assert pending["summary_en"] is None
    result = voice.get(mid)
    assert result["status"] == "done"
    assert result["result"]["summary"] == "Pickup is provisional."
    assert result["result"]["fields"]["confirmed"] is False
    assert result["result"]["fields"]["booking_reference"] is None
    inbox.mark_handled(reply["id"], "Gate availability still pending.")
    assert voice.get(mid)["result"]["summary"] == "Gate availability still pending."
    last_count = len(requests)
    assert voice.collect() == []
    assert len(requests) == last_count


def test_demo_call_only_dials_configured_number_once(call, provider):
    requests, responses = provider
    rules.cfg("voice").update(dial_out=True, demo_number="+15555550123")
    responses["/convai/phone-numbers"] = [{"provider": "twilio", "phone_number_id": "phone_fixture"}]
    responses["/convai/twilio/outbound-call"] = {"success": True, "conversation_id": "conv_fixture"}
    assert voice.prepare(call["id"])["status"] == "in_progress"
    assert voice.prepare(call["id"])["status"] == "in_progress"
    payloads = [r[-1] for r in requests if r[1].endswith("outbound-call")]
    assert payloads == [{"agent_id": "agent_test", "agent_phone_number_id": "phone_fixture",
                         "to_number": "+15555550123"}]
