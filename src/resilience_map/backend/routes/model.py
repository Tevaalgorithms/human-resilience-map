"""Authenticated model status and an explicit live connection check."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from resilience_map.agents.llm import get_llm
from resilience_map.agents.plan import RequestPlan
from resilience_map.agents.prompts import render
from resilience_map.backend.auth import current_user
from resilience_map.core.config import Settings, get_settings
from resilience_map.core.errors import ValidationFailed

router = APIRouter(prefix="/v1/model", tags=["model"], dependencies=[Depends(current_user)])


@router.get("")
def model_status(settings: Settings = Depends(get_settings)) -> dict[str, Any]:  # noqa: B008
    return {
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "configured": settings.llm_configured,
        "mode": "offline"
        if settings.llm_provider == "stub"
        else ("live" if settings.llm_configured else "needs_configuration"),
        "model_roles": [
            "request interpretation",
            "result explanation",
            "area question planning",
            "data quality advice",
            "scenario advice",
        ],
        "calculation_roles": ["scoring", "routing", "optimization", "verification"],
        "notice": "Configured means server credentials are present; use Check model connection to verify access.",
    }


@router.post("/check")
def check_model(settings: Settings = Depends(get_settings)) -> dict[str, Any]:  # noqa: B008
    if settings.llm_provider == "stub":
        raise HTTPException(
            409, "Offline test mode is enabled. Select a real model provider on the server."
        )
    try:
        response = get_llm(settings).complete(
            render(
                "interpret_request",
                study_area_id="connection-check",
                known_constraints_json="{}",
                question="Which are the most vulnerable tracts? top 3",
            ),
            settings.llm_max_output_tokens,
        )
        plan = RequestPlan.model_validate_json(response.text)
        if plan.intent != "vulnerability_ranking" or plan.top_n != 3:
            raise ValidationFailed(
                "The model responded but did not pass the request-interpretation check."
            )
    except (ValidationFailed, ValueError) as exc:
        raise HTTPException(503, str(exc)) from None
    return {
        "status": "connected",
        "provider": settings.llm_provider,
        "model": response.model or settings.llm_model,
        "response_id": response.response_id,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
    }
