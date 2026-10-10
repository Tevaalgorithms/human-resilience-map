"""API request/response schemas."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from resilience_map.core.geography import PlanningBoundingBox
from resilience_map.core.models import AccessModel, TravelMode


class ConstraintsIn(BaseModel):
    """Optional structured constraints; anything omitted may be extracted from the question or
    requested through a clarification."""

    model_config = ConfigDict(extra="forbid")
    bbox: PlanningBoundingBox | None = None
    access_model: AccessModel | None = None
    num_centers: int | None = Field(default=None, ge=1, le=3)
    travel_mode: TravelMode | None = None
    threshold_minutes: float | None = Field(default=None, gt=0, le=120)
    budget_usd: float | None = Field(default=None, ge=0)
    candidate_site_ids: list[str] | None = Field(default=None, max_length=50)

    tract_id: str | None = Field(default=None, pattern=r"^[0-9A-Za-z-]{3,20}$")
    top_n: int | None = Field(default=None, ge=1, le=25)


ScenarioId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")]


class ScenarioAlternativeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: ScenarioId
    constraints: ConstraintsIn


class ComparisonIn(BaseModel):
    """Explicit alternatives in addition to the primary requested scenario."""

    model_config = ConfigDict(extra="forbid")
    alternatives: list[ScenarioAlternativeIn] = Field(min_length=1, max_length=4)
    objective: (
        Literal["maximize_newly_served", "maximize_scenario_access", "minimize_cost"] | None
    ) = None

    @model_validator(mode="after")
    def unique_scenario_ids(self) -> ComparisonIn:
        identifiers = [alternative.scenario_id for alternative in self.alternatives]
        if "requested" in identifiers or len(identifiers) != len(set(identifiers)):
            raise ValueError("Alternative scenario IDs must be unique and cannot be 'requested'")
        return self


class SensitivityVariantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant_id: ScenarioId
    planning_index_weight_multipliers: dict[str, float] = Field(default_factory=dict, max_length=8)
    routing_speed_multiplier: float | None = Field(default=None, ge=0.5, le=1.5)
    candidate_capacity_multiplier: float | None = Field(default=None, ge=0.5, le=1.5)

    @model_validator(mode="after")
    def require_variation(self) -> SensitivityVariantIn:
        if not (
            self.planning_index_weight_multipliers
            or self.routing_speed_multiplier is not None
            or self.candidate_capacity_multiplier is not None
        ):
            raise ValueError("A sensitivity variant must vary at least one supported parameter")
        for key, value in self.planning_index_weight_multipliers.items():
            if not key or len(key) > 40 or not 0.5 <= value <= 1.5:
                raise ValueError(
                    "Weight multiplier names must be 1-40 characters and values 0.5-1.5"
                )
        return self


class UncertaintyIn(BaseModel):
    """Explicit deterministic variants in addition to the primary requested scenario."""

    model_config = ConfigDict(extra="forbid")
    variants: list[SensitivityVariantIn] = Field(min_length=1, max_length=4)
    unsupported_parameters: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=5
    )

    @model_validator(mode="after")
    def unique_variant_ids(self) -> UncertaintyIn:
        identifiers = [variant.variant_id for variant in self.variants]
        if "reference" in identifiers or len(identifiers) != len(set(identifiers)):
            raise ValueError("Sensitivity variant IDs must be unique and cannot be 'reference'")
        return self


class AnalysisCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=3, max_length=1000)
    study_area_id: str = Field(pattern=r"^[a-z0-9-]{2,64}$")
    dataset_version_id: str | None = None  # default: latest published (then pinned)
    constraints: ConstraintsIn = Field(default_factory=ConstraintsIn)
    comparison: ComparisonIn | None = None
    uncertainty: UncertaintyIn | None = None


class AnalysisAccepted(BaseModel):
    job_id: str
    status: str
    status_url: str


class Clarification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bbox: PlanningBoundingBox | None = None
    access_model: AccessModel | None = None
    num_centers: int | None = Field(default=None, ge=1, le=3)
    travel_mode: TravelMode | None = None
    threshold_minutes: float | None = Field(default=None, gt=0, le=120)
    budget_usd: float | None = Field(default=None, ge=0)
    tract_id: str | None = Field(default=None, pattern=r"^[0-9A-Za-z-]{3,20}$")


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_id: str = Field(pattern=r"^[0-9A-Za-z-]{1,40}$")
    decision: Literal["approved", "rejected", "needs_followup"]
    notes: str = Field(default="", max_length=1000)


class AnalysisStatus(BaseModel):
    job_id: str
    status: Literal["queued", "running", "needs_input", "completed", "incomplete", "failed"]
    progress: str
    question: str
    study_area_id: str
    dataset_version_id: str
    scoring_version: str
    clarification: dict[str, Any] | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str
    updated_at: str


class PreparationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str = Field(pattern=r"^[0-9]{2}$")
    county: str = Field(pattern=r"^[0-9]{3}$")
    bbox: PlanningBoundingBox | None = None
    include_cooling: bool = False
