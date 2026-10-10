from __future__ import annotations

import pytest

from resilience_map.core.models import Indicators
from resilience_map.risk.scoring import SCORING_V1, score_tract, sensitivity

FULL = Indicators(
    surface_temp_anomaly_c=4.0,
    pct_age_65_plus=15.0,
    median_income_usd=60_000.0,
    canopy_pct=25.0,
    transit_access_index=0.5,
)


def test_weights_sum_to_one():
    assert sum(s.weight for s in SCORING_V1.specs) == pytest.approx(1.0)


def test_score_bounds_and_best_worst_cases():
    best = Indicators(
        surface_temp_anomaly_c=0,
        pct_age_65_plus=0,
        median_income_usd=150_000,
        canopy_pct=60,
        transit_access_index=1,
    )
    worst = Indicators(
        surface_temp_anomaly_c=8,
        pct_age_65_plus=35,
        median_income_usd=20_000,
        canopy_pct=0,
        transit_access_index=0,
    )
    assert score_tract(best, 0.0).score == pytest.approx(100.0)
    assert score_tract(worst, 5.0).score == pytest.approx(0.0)
    # out-of-range raw values are clamped, never producing scores outside 0-100
    extreme = Indicators(
        surface_temp_anomaly_c=99,
        pct_age_65_plus=99,
        median_income_usd=0,
        canopy_pct=0,
        transit_access_index=0,
    )
    assert 0 <= score_tract(extreme, 50.0).score <= 100


def test_components_sum_to_score_and_are_reproducible():
    r1, r2 = score_tract(FULL, 1.2), score_tract(FULL, 1.2)
    assert r1 == r2
    assert sum(c.contribution for c in r1.components) == pytest.approx(r1.score, abs=1e-3)
    assert r1.scoring_version == "scoring-v1"


def test_more_canopy_means_higher_resilience():
    more = FULL.model_copy(update={"canopy_pct": 45.0})
    assert score_tract(more, 1.0).score > score_tract(FULL, 1.0).score


def test_missing_indicator_renormalizes_weights():
    partial = FULL.model_copy(update={"transit_access_index": None})
    r = score_tract(partial, 1.0)
    assert "transit_access_index" in r.missing
    assert r.score is not None
    assert sum(c.weight for c in r.components) == pytest.approx(1.0)


def test_insufficient_data_withholds_score():
    sparse = Indicators(canopy_pct=20.0)
    r = score_tract(sparse, None)
    assert r.score is None and len(r.missing) == 5


def test_sensitivity_range_contains_base_score():
    base = score_tract(FULL, 1.0).score
    lo, hi = sensitivity(FULL, 1.0)
    assert lo <= base <= hi
