import httpx2 as httpx
import openai
import pytest

from core import llm_health


def _err(cls, status, code):
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    body = {"error": {"code": code, "message": code}}
    return cls(code, response=httpx.Response(status, json=body, request=req), body=body)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_health, "STATUS", tmp_path / "llm_status.json")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-aaaaaa")


def test_out_of_credit_pauses_and_new_key_resumes(monkeypatch):
    e = llm_health.record_failure(_err(openai.RateLimitError, 429, "insufficient_quota"))
    assert isinstance(e, llm_health.LLMUnavailable) and e.kind == "out_of_credit"
    assert not llm_health.available()
    with pytest.raises(llm_health.LLMUnavailable):
        llm_health.guard()
    assert "new key" in str(llm_health.MESSAGES["out_of_credit"]) or "OPENAI_API_KEY" in llm_health.MESSAGES["out_of_credit"]
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-bbbbbb")  # user pastes a new key
    assert llm_health.available()
    llm_health.guard()


def test_rate_limit_is_temporary_and_invalid_key_is_named():
    assert llm_health.classify(_err(openai.RateLimitError, 429, "rate_limit_exceeded")) == "rate_limited"
    assert llm_health.classify(_err(openai.AuthenticationError, 401, "invalid_api_key")) == "key_invalid"
    assert llm_health.classify(ValueError("unrelated")) is None


def test_missing_key_is_reported(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(llm_health.LLMUnavailable) as info:
        llm_health.guard()
    assert info.value.kind == "key_invalid"
