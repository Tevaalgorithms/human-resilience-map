"""Strict, bounded routing contract for area questions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AreaPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent: Literal[
        "vulnerability_ranking",
        "tract_explanation",
        "data_quality",
        "scenario_advice",
        "unsupported",
    ]
    tract_id: str | None
    top_n: int | None = Field(ge=1, le=25)
    clarification: str | None
