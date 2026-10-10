"""Typed LangGraph workflow state. Everything here must be JSON-serializable (checkpointed)."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

Status = Literal["running", "completed", "incomplete"]


class Usage(TypedDict):
    llm_calls: int
    tokens: int
    tool_calls: int
    analysis_runs: int
    solver_seconds: float


class WorkflowState(TypedDict, total=False):
    # inputs
    job_id: str
    question: str
    study_area_id: str
    dataset_version_id: str
    explicit: dict[str, Any]  # structured constraints supplied by the UI / clarifications
    comparison_request: dict[str, Any] | None
    uncertainty_request: dict[str, Any] | None
    started_at: float  # epoch seconds; used for the wall-clock limit
    # planning
    plan: dict[str, Any]
    intent: str
    constraints: dict[str, Any]
    needs: list[str]  # human-readable reasons the plan cannot run yet
    clarify_round: int
    # analysis
    analysis: dict[str, Any]
    verification: dict[str, Any]
    readiness: dict[str, Any]
    comparison: dict[str, Any] | None
    uncertainty: dict[str, Any] | None
    explanation: str
    explanation_source: Literal["llm", "deterministic"]
    narrative_check: dict[str, Any]
    # outcome
    status: Status
    incomplete_reason: str
    usage: Usage
    prompts: list[str]
    model_calls: list[dict[str, Any]]
    result: dict[str, Any]
