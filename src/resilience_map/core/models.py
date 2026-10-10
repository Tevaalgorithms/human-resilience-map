"""Domain models shared by ingestion, scoring, optimization, agents and API."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from resilience_map.core.geography import PlanningBoundingBox

AccessModel = Literal["straight_line_proxy", "road_network"]

TravelMode = Literal["walk", "bike", "drive"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Indicators(Frozen):
    surface_temp_anomaly_c: float | None = None
    pct_age_65_plus: float | None = None
    median_income_usd: float | None = None
    median_income_moe_usd: float | None = None
    canopy_pct: float | None = None
    transit_access_index: float | None = None


class Tract(Frozen):
    tract_id: str
    name: str
    population: int
    lon: float  # centroid (population is allocated to the centroid; see docs/pilot_scope.md)
    lat: float
    indicators: Indicators


class CoolingCenter(Frozen):
    center_id: str
    name: str
    capacity: int | None = Field(default=None, ge=0)
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    hours: str | None = None
    last_confirmed: date | None = None
    source: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    opening_status: Literal["unknown", "confirmed_open", "confirmed_closed"] = "unknown"


class CandidateSite(Frozen):
    site_id: str
    name: str
    capacity: int
    cost_usd: float
    lon: float
    lat: float
    accessible: bool = True


class DatasetBundle(Frozen):
    """Everything an analysis needs, pinned to one published dataset version."""

    dataset_version_id: str
    version: str
    study_area_id: str
    scoring_version: str
    tracts: list[Tract]
    centers: list[CoolingCenter]
    candidates: list[CandidateSite]
    source_manifest: list[dict[str, object]] = Field(default_factory=list)
    roads: dict[str, Any] | None = None
    inventory_status: Literal["unknown", "partial", "verified", "confirmed_absent"] = "unknown"


class ScenarioConstraints(Frozen):
    bbox: PlanningBoundingBox | None = None

    access_model: AccessModel = "straight_line_proxy"
    num_centers: int = Field(ge=1, le=3)
    travel_mode: TravelMode
    threshold_minutes: float = Field(gt=0, le=120)
    budget_usd: float | None = Field(default=None, ge=0)
    candidate_site_ids: list[str] | None = None


class AnalysisOverrides(Frozen):
    """Bounded deterministic sensitivity parameters applied consistently to analysis/checks."""

    planning_index_weight_multipliers: dict[str, float] = Field(default_factory=dict)
    routing_speed_multiplier: float = Field(default=1.0, ge=0.5, le=1.5)
    candidate_capacity_multiplier: float = Field(default=1.0, ge=0.5, le=1.5)
