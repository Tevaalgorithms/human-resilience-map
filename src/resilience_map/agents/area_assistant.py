"""Read-only area assistant: bounded planning, approved tools and grounded advice."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from resilience_map.agents.area_plan import AreaPlan
from resilience_map.agents.llm import LLMClient
from resilience_map.agents.prompts import render
from resilience_map.agents.tools import Toolbox
from resilience_map.core.config import Settings
from resilience_map.core.errors import ValidationFailed
from resilience_map.core.geography import PlanningBoundingBox
from resilience_map.core.models import DatasetBundle, ScenarioConstraints
from resilience_map.gis.selection import MAX_SELECTION_TRACTS, select_bundle
from resilience_map.verification.narrative import check_narrative
from resilience_map.verification.summary import summarize


class AreaQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    study_area_id: str = Field(min_length=1, max_length=100)
    dataset_version_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    county: str | None = Field(default=None, pattern=r"^\d{5}$")
    bbox: PlanningBoundingBox | None = None
    question: str = Field(min_length=1, max_length=2000)
    previous_questions: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("dataset_version_id")
    @classmethod
    def valid_version(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("question")
    @classmethod
    def nonempty_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a question")
        return value.strip()

    # Earlier user questions are context only; answers never become evidence.


def answer_area(
    body: AreaQuestion, bundle: DatasetBundle, llm: LLMClient, settings: Settings
) -> dict[str, Any]:
    if any(len(q) > 2000 for q in body.previous_questions):
        raise ValidationFailed("Earlier questions exceed the context limit")
    if (
        bundle.study_area_id != body.study_area_id
        or bundle.dataset_version_id != body.dataset_version_id
    ):
        raise ValidationFailed("Dataset does not match the selected area/version")
    if body.county:
        bundle = bundle.model_copy(
            update={"tracts": [t for t in bundle.tracts if t.tract_id.startswith(body.county)]}
        )
    if body.bbox:
        try:
            bundle = select_bundle(bundle, body.bbox)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from None
    if not bundle.tracts:
        raise ValidationFailed(
            "No tracts are selected. Pick a county or draw a planning box that covers some tracts."
        )
    if len(bundle.tracts) > MAX_SELECTION_TRACTS:
        raise ValidationFailed(
            f"Your selection has {len(bundle.tracts):,} tracts; the assistant works with up to "
            f"{MAX_SELECTION_TRACTS}. Choose a smaller county, or zoom in and draw a planning "
            "box on the map to narrow it."
        )
    context = {
        "study_area_id": body.study_area_id,
        "dataset_version_id": body.dataset_version_id,
        "county": body.county,
        "bbox": body.bbox,
        "previous_questions": body.previous_questions,
    }
    calls: list[dict[str, Any]] = []

    def call(name: str, evidence: dict[str, Any]) -> str:
        variables = {
            "context_json": json.dumps(context),
            "question": body.question,
            "evidence_json": json.dumps(evidence),
        }
        prompt = render(name, **variables)
        used = sum(c["input_tokens"] + c["output_tokens"] for c in calls)
        estimated = (len(prompt.system) + len(prompt.user)) // 3 + settings.llm_max_output_tokens
        if (
            len(calls) >= min(2, settings.llm_max_calls_per_job)
            or used + estimated > settings.llm_max_tokens_per_job
        ):
            raise ValidationFailed(
                "Area-question model budget exceeded; narrow the selection or shorten the context"
            )
        response = llm.complete(prompt, settings.llm_max_output_tokens)
        calls.append(
            {
                "prompt": prompt.ref,
                "provider": settings.llm_provider,
                "model": response.model or settings.llm_model,
                "response_id": response.response_id,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            }
        )
        return response.text

    try:
        plan = AreaPlan.model_validate_json(call("area_question_planner", {}))
    except ValidationError:
        raise ValidationFailed("The model returned an invalid area-question plan") from None
    if plan.intent == "unsupported":
        return {
            "answer": plan.clarification
            or "Ask about heat vulnerability, a tract, data quality or scenario planning.",
            "intent": plan.intent,
            "scope": context,
            "evidence": {},
            "model_calls": calls,
            "answer_source": "planner",
            "tool_calls": 0,
        }
    toolbox = Toolbox(bundle, {"max_tool_calls": 2})
    if plan.intent == "vulnerability_ranking":
        evidence = toolbox.call("rank_vulnerable_tracts", {"top_n": plan.top_n or 5})
        answer = call("explain_results", evidence)
    elif plan.intent == "tract_explanation":
        if not plan.tract_id:
            raise ValidationFailed("Specify the tract ID to explain")
        evidence = toolbox.call("explain_tract", {"tract_id": plan.tract_id})
        answer = call("explain_results", evidence)
    else:
        constraints = ScenarioConstraints(num_centers=1, travel_mode="walk", threshold_minutes=15)
        evidence = toolbox.call(
            "assess_data_readiness",
            {"operation": "site_selection", "constraints": constraints.model_dump(mode="json")},
        )
        evidence["scope_notice"] = (
            "Selected tract centroids; centers and destinations from the full published snapshot retained for access context."
        )
        evidence["reference_scenario"] = constraints.model_dump(mode="json")
        evidence["reference_scenario_notice"] = (
            "Readiness reference only; not a user-selected or optimized scenario."
        )
        answer = call(
            "data_quality_advisor" if plan.intent == "data_quality" else "scenario_advisor",
            evidence,
        )
    check = check_narrative(answer, evidence)
    source = "llm"
    if not check.ok:
        source = "verified_evidence_fallback"
        answer = (
            summarize(evidence)
            if plan.intent in {"vulnerability_ranking", "tract_explanation"}
            else " ".join(
                [
                    f"Readiness: {evidence['status']}.",
                    *[r["message"] for r in evidence["reasons"]],
                    *evidence["suggested_next_actions"],
                ]
            )
        )
    return {
        "answer": answer,
        "intent": plan.intent,
        "scope": context,
        "evidence": evidence,
        "model_calls": calls,
        "answer_source": source,
        "tool_calls": toolbox.calls,
        "grounding_check": {
            "ok": check.ok,
            "unsupported_numbers": check.unsupported_numbers,
            "unsupported_ids": check.unsupported_ids,
        },
        "notice": "Historical planning evidence. Numeric and identifier checks do not validate every prose claim; review the evidence.",
    }
