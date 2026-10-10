"""Shared structured-output contract for the model's request interpretation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class RequestPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    intent: Literal["site_selection", "tract_explanation", "vulnerability_ranking", "unsupported"]
    num_centers: int | None
    travel_mode: Literal["walk", "bike", "drive"] | None
    access_model: Literal["straight_line_proxy", "road_network"] | None
    threshold_minutes: float | None
    budget_usd: float | None
    tract_id: str | None
    top_n: int | None
    reason_unsupported: str | None
