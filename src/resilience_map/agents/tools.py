"""Approved analytical tools. The workflow can call ONLY these typed functions: no SQL, no
code execution. Each tool has a pydantic input schema and returns plain JSON-serializable data."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from resilience_map.agents.readiness import FreshnessPolicy, assess_readiness
from resilience_map.core.errors import LimitExceeded, ValidationFailed
from resilience_map.core.models import AnalysisOverrides, DatasetBundle, ScenarioConstraints
from resilience_map.optimization.scenario import ASSUMPTIONS, LIMITATIONS, run_site_selection
from resilience_map.risk.scoring import sensitivity
from resilience_map.risk.service import score_dataset


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SiteSelectionIn(_In):
    constraints: ScenarioConstraints
    overrides: AnalysisOverrides = Field(default_factory=AnalysisOverrides)


class ReadinessIn(_In):
    operation: str = Field(pattern=r"^(site_selection|tract_explanation|vulnerability_ranking)$")
    constraints: ScenarioConstraints | None = None
    freshness_policy: FreshnessPolicy = Field(default_factory=FreshnessPolicy)


class TractExplanationIn(_In):
    tract_id: str = Field(pattern=r"^[0-9A-Za-z-]{3,20}$")


class RankingIn(_In):
    top_n: int = Field(default=5, ge=1, le=25)


def _select_cooling_sites(
    bundle: DatasetBundle, args: SiteSelectionIn, limits: dict[str, Any]
) -> dict[str, Any]:
    try:
        return run_site_selection(
            bundle,
            args.constraints,
            limits["solver_max_seconds"],
            limits["max_candidate_sites"],
            args.overrides,
        )
    except ValueError as e:
        raise ValidationFailed(str(e)) from e


def _explain_tract(
    bundle: DatasetBundle, args: TractExplanationIn, limits: dict[str, Any]
) -> dict[str, Any]:
    scores = score_dataset(bundle)
    tract = next((t for t in bundle.tracts if t.tract_id == args.tract_id), None)
    if tract is None:
        raise ValidationFailed(f"Tract {args.tract_id} is not in dataset {bundle.version}")
    km, res = scores[tract.tract_id]
    sens = sensitivity(tract.indicators, km)
    return {
        "intent": "tract_explanation",
        "dataset_version": bundle.version,
        "dataset_version_id": bundle.dataset_version_id,
        "scoring_version": bundle.scoring_version,
        "tract": {
            "tract_id": tract.tract_id,
            "name": tract.name,
            "population": tract.population,
            "score": res.score,
            "nearest_center_km": None if km is None else round(km, 3),
            "missing_indicators": res.missing,
            "weight_sensitivity_range": None
            if sens is None
            else [round(sens[0], 2), round(sens[1], 2)],
            "income_margin_of_error_usd": tract.indicators.median_income_moe_usd,
            "components": [
                {
                    "key": c.key,
                    "label": c.label,
                    "raw_value": c.raw_value,
                    "unit": c.unit,
                    "contribution": c.contribution,
                    "weight": round(c.weight, 4),
                }
                for c in res.components
            ],
        },
        "assumptions": [
            *ASSUMPTIONS[-1:],
            "Surface temperature anomaly is land-surface temperature, not air temperature.",
        ],
        "limitations": LIMITATIONS[1:],
        "sources": bundle.source_manifest,
    }


def _rank_tracts(bundle: DatasetBundle, args: RankingIn, limits: dict[str, Any]) -> dict[str, Any]:
    scores = score_dataset(bundle)
    names = {t.tract_id: t for t in bundle.tracts}
    ranked = sorted(
        ((tid, r.score) for tid, (_, r) in scores.items() if r.score is not None),
        key=lambda x: (x[1], x[0]),
    )[: args.top_n]
    return {
        "intent": "vulnerability_ranking",
        "returned_tract_count": len(ranked),
        "ranking_rule": "Ascending resilience score, then tract ID. Lower resilience score means greater vulnerability; preserve this order.",
        "dataset_version": bundle.version,
        "dataset_version_id": bundle.dataset_version_id,
        "scoring_version": bundle.scoring_version,
        "ranking": [
            {
                "tract_id": tid,
                "name": names[tid].name,
                "population": names[tid].population,
                "score": s,
            }
            for tid, s in ranked
        ],
        "tracts_without_score": sorted(tid for tid, (_, r) in scores.items() if r.score is None),
        "assumptions": ASSUMPTIONS[-1:],
        "limitations": LIMITATIONS[1:],
        "sources": bundle.source_manifest,
    }


def _assess_readiness(
    bundle: DatasetBundle, args: ReadinessIn, limits: dict[str, Any]
) -> dict[str, Any]:
    del limits
    return assess_readiness(
        bundle, args.operation, args.constraints, args.freshness_policy
    ).model_dump(mode="json")


_TOOLS: dict[
    str, tuple[type[_In], Callable[[DatasetBundle, Any, dict[str, Any]], dict[str, Any]]]
] = {
    "select_cooling_sites": (SiteSelectionIn, _select_cooling_sites),
    "explain_tract": (TractExplanationIn, _explain_tract),
    "rank_vulnerable_tracts": (RankingIn, _rank_tracts),
    "assess_data_readiness": (ReadinessIn, _assess_readiness),
}
INTENT_TOOL = {
    "site_selection": "select_cooling_sites",
    "tract_explanation": "explain_tract",
    "vulnerability_ranking": "rank_vulnerable_tracts",
}


class Toolbox:
    """Bounded tool invocation: schema-validated inputs and a per-job call budget."""

    def __init__(self, bundle: DatasetBundle, limits: dict[str, Any], used: int = 0) -> None:
        self._bundle = bundle
        self._limits = limits
        self.calls = used

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in _TOOLS:
            raise ValidationFailed(f"Unknown tool {name!r}")
        if self.calls >= self._limits["max_tool_calls"]:
            raise LimitExceeded("Tool-call budget exhausted")
        self.calls += 1
        model, fn = _TOOLS[name]
        return fn(self._bundle, model.model_validate(arguments), self._limits)
