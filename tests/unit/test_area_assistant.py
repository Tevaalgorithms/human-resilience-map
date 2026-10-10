import json

import httpx
import pytest

from resilience_map.agents.area_assistant import AreaQuestion, answer_area
from resilience_map.agents.llm import LLMResponse, OpenAILLM
from resilience_map.agents.prompts import render
from resilience_map.core.config import Settings
from resilience_map.core.errors import ValidationFailed

VERSION = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class FakeModel:
    def __init__(
        self, intent, text="Review the locally verified inventory before planning.", tract=None
    ):
        self.intent, self.text, self.tract = intent, text, tract
        self.prompts = []

    def complete(self, prompt, max_tokens):
        self.prompts.append(prompt)
        text = (
            json.dumps(
                {
                    "intent": self.intent,
                    "tract_id": self.tract,
                    "top_n": 3,
                    "clarification": "Please ask about historical heat planning.",
                }
            )
            if len(self.prompts) == 1
            else self.text
        )
        return LLMResponse(text, 100, 50, "actual-model", "response-test")


def request(**kwargs):
    return AreaQuestion(
        study_area_id="synthetic-metro",
        dataset_version_id=VERSION,
        question="What data is missing?",
        **kwargs,
    )


def run(bundle, model, **kwargs):
    return answer_area(
        request(**kwargs),
        bundle.model_copy(update={"dataset_version_id": VERSION}),
        model,
        Settings(_env_file=None, llm_provider="stub"),
    )


def test_quality_advisor_uses_readiness_and_real_call_metadata(bundle):
    model = FakeModel("data_quality")
    result = run(bundle, model)
    assert [p.name for p in model.prompts] == ["area_question_planner", "data_quality_advisor"]
    assert result["tool_calls"] == 1
    assert result["evidence"]["dataset_version_id"] == VERSION
    assert result["model_calls"][1]["model"] == "actual-model"
    assert result["answer_source"] == "llm"


def test_scenario_advice_does_not_run_solver(bundle):
    result = run(bundle, FakeModel("scenario_advice"))
    assert result["model_calls"][1]["prompt"] == "scenario_advisor@v1"
    assert (
        "not a user-selected or optimized scenario"
        in result["evidence"]["reference_scenario_notice"]
    )
    assert "selected_sites" not in result["evidence"]


def test_invented_number_falls_back_to_readiness(bundle):
    result = run(bundle, FakeModel("data_quality", "There are 987654321 open centers."))
    assert result["answer_source"] == "verified_evidence_fallback"
    assert "987654321" not in result["answer"]
    assert result["grounding_check"]["ok"] is False


def test_county_filter_and_out_of_scope_tract(bundle):
    # Keep only one of two counties; a tract from the excluded county cannot be explained.
    first = bundle.tracts[0].model_copy(update={"tract_id": "48453000101"})
    second = bundle.tracts[1].model_copy(update={"tract_id": "06037000101"})
    bundle = bundle.model_copy(update={"tracts": [first, second]})
    result = run(bundle, FakeModel("vulnerability_ranking"), county="48453")
    assert [r["tract_id"] for r in result["evidence"]["ranking"]] == [first.tract_id]
    with pytest.raises(ValidationFailed, match="not in dataset"):
        run(bundle, FakeModel("tract_explanation", tract=second.tract_id), county="48453")


def test_wrong_dataset_and_empty_scope_fail_before_model_call(bundle):
    model = FakeModel("data_quality")
    with pytest.raises(ValidationFailed, match="does not match"):
        answer_area(request(), bundle, model, Settings(_env_file=None))
    with pytest.raises(ValidationFailed, match="between 1 and 500"):
        run(bundle, model, county="12345")
    assert not model.prompts


def test_unsupported_is_bounded_and_history_is_context_only(bundle):
    model = FakeModel("unsupported")
    result = run(bundle, model, previous_questions=["What data is missing?"])
    assert result["tool_calls"] == 0 and len(model.prompts) == 1
    assert "What data is missing?" in model.prompts[0].user
    assert result["evidence"] == {}


def test_planner_uses_strict_openai_schema():
    plan = {"intent": "data_quality", "tract_id": None, "top_n": None, "clarification": None}

    def handle(req):
        fmt = json.loads(req.content)["text"]["format"]
        assert fmt["strict"] and fmt["schema"]["additionalProperties"] is False
        assert set(fmt["schema"]["required"]) == set(plan)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(plan)}],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )

    llm = OpenAILLM(
        Settings(_env_file=None, llm_provider="openai", OPENAI_API_KEY="test-only"),
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    prompt = render(
        "area_question_planner", context_json="{}", question="What is missing?", evidence_json="{}"
    )
    assert json.loads(llm.complete(prompt, 1024).text) == plan


def test_authenticated_api_pins_dataset_and_rejects_offline(bundle, monkeypatch):
    from fastapi.testclient import TestClient

    from resilience_map.backend.deps import db_session
    from resilience_map.backend.main import create_app
    from resilience_map.backend.routes import assistant
    from resilience_map.core.config import get_settings

    class FakeSession:
        released = False

        def rollback(self):
            self.released = True

    db = FakeSession()
    model = FakeModel("data_quality")
    app = create_app()
    app.dependency_overrides[db_session] = lambda: db
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, llm_provider="openai", OPENAI_API_KEY="test-only"
    )
    monkeypatch.setattr(assistant, "_resolve", lambda session, area, version: {"id": version})
    monkeypatch.setattr(
        assistant.repo,
        "load_bundle",
        lambda session, version: bundle.model_copy(update={"dataset_version_id": version}),
    )
    monkeypatch.setattr(assistant, "get_llm", lambda settings: model)
    client = TestClient(app)
    response = client.post(
        "/v1/assistant/questions",
        json=request().model_dump(mode="json"),
        headers={"X-Dev-User": "alice"},
    )
    assert response.status_code == 200
    assert response.json()["model_calls"][1]["prompt"] == "data_quality_advisor@v1"
    assert db.released
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, llm_provider="stub")
    response = client.post("/v1/assistant/questions", json=request().model_dump(mode="json"))
    assert response.status_code == 409


def test_budget_and_malformed_plan_are_explicit(bundle):
    model = FakeModel("data_quality")
    with pytest.raises(ValidationFailed, match="budget exceeded"):
        answer_area(
            request(),
            bundle.model_copy(update={"dataset_version_id": VERSION}),
            model,
            Settings(_env_file=None, llm_max_tokens_per_job=1),
        )
    assert not model.prompts

    class BadModel:
        def complete(self, prompt, max_tokens):
            return LLMResponse('{"intent":"execute_sql"}', 1, 1)

    with pytest.raises(ValidationFailed, match="invalid area-question plan"):
        run(bundle, BadModel())


def test_invalid_version_blank_question_and_empty_bbox_fail_cleanly(bundle):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AreaQuestion(study_area_id="a", dataset_version_id="-" * 36, question="q")
    with pytest.raises(ValidationError):
        AreaQuestion(study_area_id="a", dataset_version_id=VERSION, question="  ")
    with pytest.raises(ValidationFailed, match="No downloaded tract centroids"):
        run(bundle, FakeModel("data_quality"), bbox=(-105.1, 39.9, -105.0, 40.0))
