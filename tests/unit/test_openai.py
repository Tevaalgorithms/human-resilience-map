from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from resilience_map.agents.llm import OpenAILLM, get_llm
from resilience_map.agents.prompts import render
from resilience_map.backend.main import create_app
from resilience_map.core.config import Settings, get_settings
from resilience_map.core.errors import ValidationFailed

PLAN = {
    "intent": "vulnerability_ranking",
    "num_centers": None,
    "travel_mode": None,
    "access_model": None,
    "threshold_minutes": None,
    "budget_usd": None,
    "tract_id": None,
    "top_n": 3,
    "reason_unsupported": None,
}


def settings(**kwargs):
    return Settings(_env_file=None, llm_provider="openai", OPENAI_API_KEY="test-key", **kwargs)


def prompt(name="interpret_request"):
    return render(
        name,
        question="Which are the most vulnerable tracts? top 3",
        study_area_id="area",
        known_constraints_json="{}",
        evidence_json="{}",
    )


def payload(text=None, **kwargs):
    return {
        "id": "resp-test",
        "model": "gpt-5.4-mini",
        "status": "completed",
        "output": [
            {"type": "reasoning"},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text or json.dumps(PLAN)}],
            },
        ],
        "usage": {"input_tokens": 50, "output_tokens": 80},
        **kwargs,
    }


def test_responses_contract_strict_schema_and_actual_usage():
    calls = []

    def handle(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        calls.append(body)
        assert body["store"] is False and body["max_output_tokens"] == 1024
        assert body["reasoning"] == {"effort": "none"}
        if len(calls) == 1:
            schema = body["text"]["format"]
            assert schema["strict"] is True
            assert schema["schema"]["additionalProperties"] is False
            assert set(schema["schema"]["required"]) == set(PLAN)
            return httpx.Response(200, json=payload())
        assert "text" not in body
        return httpx.Response(200, json=payload("Evidence explanation."))

    client = OpenAILLM(settings(), httpx.Client(transport=httpx.MockTransport(handle)))
    response = client.complete(prompt(), 1024)
    assert json.loads(response.text) == PLAN
    assert response.input_tokens == 50 and response.output_tokens == 80
    assert response.model == "gpt-5.4-mini" and response.response_id == "resp-test"
    assert client.complete(prompt("explain_results"), 1024).text == "Evidence explanation."


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500])
def test_provider_errors_are_explicit_and_redacted(status):
    client = OpenAILLM(
        settings(),
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, json={"error": {"message": "private test-key"}})
            )
        ),
    )
    with pytest.raises(ValidationFailed) as exc:
        client.complete(prompt(), 1024)
    assert "OpenAI" in str(exc.value) and "test-key" not in str(exc.value)
    assert "private" not in str(exc.value)


@pytest.mark.parametrize(
    "response",
    [
        payload(status="incomplete"),
        payload(output=[]),
        payload(output=[{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]),
        payload(text='{"intent":"invented"}'),
        payload(usage={}),
    ],
)
def test_refusal_truncation_empty_text_and_invalid_schema_do_not_fallback(response):
    client = OpenAILLM(
        settings(),
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))),
    )
    with pytest.raises(ValidationFailed):
        client.complete(prompt(), 1024)


def test_timeout_does_not_leak_credentials_or_switch_provider():
    def fail(request):
        raise httpx.ReadTimeout("private test-key", request=request)

    client = OpenAILLM(settings(), httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(ValidationFailed, match="timeout") as exc:
        client.complete(prompt(), 1024)
    assert "test-key" not in str(exc.value)


def test_missing_credentials_fail_without_stub(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("HRM_OPENAI_API_KEY", raising=False)
    s = Settings(_env_file=None, llm_provider="openai")
    assert not s.llm_configured
    with pytest.raises(ValidationFailed, match="OPENAI_API_KEY"):
        get_llm(s)
    assert "test-key" not in repr(settings())


def test_model_status_and_explicit_connection_check(monkeypatch):
    from resilience_map.backend.routes import model

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings()

    class FakeLLM:
        def complete(self, prompt, max_tokens):
            from resilience_map.agents.llm import LLMResponse

            return LLMResponse(json.dumps(PLAN), 50, 80, "gpt-5.4-mini", "resp-test")

    monkeypatch.setattr(model, "get_llm", lambda _: FakeLLM())
    client = TestClient(app)
    status = client.get("/v1/model")
    assert status.json()["mode"] == "live" and "test-key" not in status.text
    result = client.post("/v1/model/check")
    assert result.json()["status"] == "connected"
    assert result.json()["response_id"] == "resp-test"
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, llm_provider="stub")
    assert client.get("/v1/model").json()["mode"] == "offline"
    assert client.post("/v1/model/check").status_code == 409
