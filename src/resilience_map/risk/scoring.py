"""Versioned resilience index (0-100, higher = more resilient).

The index is a *planning measure*, not a validated health prediction. Every parameter that
affects a score (reference ranges, weights, missing-data rules) lives in ``SCORING_V1`` so a
score is reproducible from (inputs, scoring_version) alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from resilience_map.core.models import Indicators

SCORING_VERSION = "scoring-v1"


# Illustrative planning bands for display only. They are NOT validated thresholds.
BANDS: tuple[tuple[float, str], ...] = (
    (40.0, "Higher vulnerability"),
    (60.0, "Moderate"),
    (101.0, "Lower vulnerability"),
)


def band_for(score: float | None) -> str:
    if score is None:
        return "Not scored"
    return next(name for upper, name in BANDS if score < upper)


@dataclass(frozen=True)
class IndicatorSpec:
    key: str
    label: str
    weight: float
    lo: float  # reference value that maps to 0 or 1 (fixed, so scores don't depend on the peer set)
    hi: float
    higher_is_better: bool
    unit: str


@dataclass(frozen=True)
class ScoringModel:
    version: str
    specs: tuple[IndicatorSpec, ...]
    min_indicators: int  # below this many available indicators, score is withheld (None)

    def spec(self, key: str) -> IndicatorSpec:
        return next(s for s in self.specs if s.key == key)


SCORING_V1 = ScoringModel(
    version=SCORING_VERSION,
    min_indicators=4,
    specs=(
        IndicatorSpec(
            "surface_temp_anomaly_c",
            "Heat exposure (surface temp anomaly)",
            0.25,
            0.0,
            8.0,
            False,
            "°C",
        ),
        IndicatorSpec("pct_age_65_plus", "Population aged 65+", 0.15, 0.0, 35.0, False, "%"),
        IndicatorSpec(
            "median_income_usd", "Median household income", 0.20, 20_000.0, 150_000.0, True, "USD"
        ),
        IndicatorSpec("canopy_pct", "Tree canopy cover", 0.15, 0.0, 60.0, True, "%"),
        IndicatorSpec("transit_access_index", "Transit access", 0.10, 0.0, 1.0, True, "index"),
        IndicatorSpec(
            "cooling_access", "Distance to nearest cooling center", 0.15, 0.0, 5.0, False, "km"
        ),
    ),
)


def model_with_weight_multipliers(
    multipliers: dict[str, float], model: ScoringModel = SCORING_V1
) -> ScoringModel:
    """Return a normalized model for deterministic sensitivity runs."""
    known = {spec.key for spec in model.specs}
    unknown = set(multipliers) - known
    if unknown:
        raise ValueError(f"Unsupported planning-index weight(s): {sorted(unknown)}")
    if any(not math.isfinite(value) or not 0.5 <= value <= 1.5 for value in multipliers.values()):
        raise ValueError("Weight multipliers must be finite values from 0.5 to 1.5")
    weighted = [(spec, spec.weight * multipliers.get(spec.key, 1.0)) for spec in model.specs]
    total = sum(weight for _, weight in weighted)
    if total <= 0:
        raise ValueError("Planning-index weights must have a positive total")
    return ScoringModel(
        version=model.version,
        specs=tuple(
            IndicatorSpec(
                spec.key,
                spec.label,
                weight / total,
                spec.lo,
                spec.hi,
                spec.higher_is_better,
                spec.unit,
            )
            for spec, weight in weighted
        ),
        min_indicators=model.min_indicators,
    )


def model_weight_provenance(model: ScoringModel) -> dict[str, float]:
    return {spec.key: round(spec.weight, 8) for spec in model.specs}


@dataclass(frozen=True)
class Component:
    key: str
    label: str
    raw_value: float | None
    unit: str
    normalized: float | None  # 0..1, 1 = most resilient
    weight: float  # effective weight after missing-data renormalization (0 if missing)
    contribution: float | None  # points out of 100


@dataclass(frozen=True)
class ScoreResult:
    score: float | None
    components: list[Component]
    missing: list[str]
    scoring_version: str


def _normalize(value: float, spec: IndicatorSpec) -> float:
    span = spec.hi - spec.lo
    frac = min(1.0, max(0.0, (value - spec.lo) / span))
    return frac if spec.higher_is_better else 1.0 - frac


def score_tract(
    indicators: Indicators,
    nearest_center_km: float | None,
    model: ScoringModel = SCORING_V1,
) -> ScoreResult:
    """Score one tract.

    Missing-data rule: unavailable indicators are excluded and the remaining weights are
    renormalized to sum to 1; if fewer than ``model.min_indicators`` are available the score
    is withheld (``None``) rather than guessed.
    """
    raw: dict[str, float | None] = {
        **indicators.model_dump(),
        "cooling_access": nearest_center_km,
    }
    available = [s for s in model.specs if raw.get(s.key) is not None]
    missing = [s.key for s in model.specs if raw.get(s.key) is None]
    total_w = sum(s.weight for s in available)

    components: list[Component] = []
    score_sum = 0.0
    sufficient = len(available) >= model.min_indicators and total_w > 0
    for s in model.specs:
        value = raw.get(s.key)
        if value is None or not sufficient:
            components.append(Component(s.key, s.label, value, s.unit, None, 0.0, None))
            continue
        norm = _normalize(float(value), s)
        eff_w = s.weight / total_w
        contribution = 100.0 * eff_w * norm
        score_sum += contribution
        components.append(
            Component(s.key, s.label, float(value), s.unit, norm, eff_w, round(contribution, 4))
        )
    return ScoreResult(
        score=round(score_sum, 4) if sufficient else None,
        components=components,
        missing=missing,
        scoring_version=model.version,
    )


def sensitivity(
    indicators: Indicators,
    nearest_center_km: float | None,
    model: ScoringModel = SCORING_V1,
    perturbation: float = 0.2,
) -> tuple[float, float] | None:
    """Score range when each weight is perturbed by ±``perturbation`` (one at a time)."""
    base = score_tract(indicators, nearest_center_km, model)
    if base.score is None:
        return None
    scores = [base.score]
    for i in range(len(model.specs)):
        for sign in (-1, 1):
            specs = list(model.specs)
            s = specs[i]
            specs[i] = IndicatorSpec(
                s.key,
                s.label,
                s.weight * (1 + sign * perturbation),
                s.lo,
                s.hi,
                s.higher_is_better,
                s.unit,
            )
            alt = score_tract(
                indicators,
                nearest_center_km,
                ScoringModel(model.version, tuple(specs), model.min_indicators),
            )
            if alt.score is not None:
                scores.append(alt.score)
    return min(scores), max(scores)
