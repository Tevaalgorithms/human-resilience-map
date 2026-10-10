from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from resilience_map.agents.readiness import FreshnessPolicy, assess_readiness
from resilience_map.backend.schemas import AnalysisCreate
from resilience_map.core.models import CandidateSite, ScenarioConstraints


def constraints(candidate_ids: list[str] | None = None) -> ScenarioConstraints:
    return ScenarioConstraints(
        num_centers=1,
        travel_mode="walk",
        threshold_minutes=15,
        candidate_site_ids=candidate_ids,
    )


def test_readiness_distinguishes_unknown_confirmed_absent_and_invalid_inventory(bundle):
    policy = FreshnessPolicy()
    now = datetime(2026, 10, 9, tzinfo=UTC)
    unknown = assess_readiness(bundle, "site_selection", constraints(), policy, now)
    absent = assess_readiness(
        bundle.model_copy(update={"centers": [], "inventory_status": "confirmed_absent"}),
        "site_selection",
        constraints(),
        policy,
        now,
    )
    invalid = assess_readiness(
        bundle.model_copy(update={"centers": [], "inventory_status": "verified"}),
        "site_selection",
        constraints(),
        policy,
        now,
    )
    assert unknown.status == "exploratory"
    assert "inventory_unknown" in {reason.code for reason in unknown.reasons}
    assert absent.status in {"ready", "exploratory"}
    assert "inventory_confirmed_absent" in {reason.code for reason in absent.reasons}
    assert invalid.status == "blocked"
    assert "verified_inventory_empty" in {reason.code for reason in invalid.reasons}


def test_invalid_siting_inputs_do_not_block_vulnerability_ranking(bundle):
    no_candidates = bundle.model_copy(update={"candidates": []})
    siting = assess_readiness(no_candidates, "site_selection", constraints(), FreshnessPolicy())
    ranking = assess_readiness(no_candidates, "vulnerability_ranking", None, FreshnessPolicy())
    assert siting.status == "blocked"
    assert ranking.status != "blocked"


def test_requested_candidate_subset_controls_resource_limit(bundle):
    requested = bundle.candidates[0]
    candidates = [requested] + [
        CandidateSite(
            site_id=f"EXTRA-{index}",
            name=f"Extra {index}",
            capacity=100,
            cost_usd=100,
            lon=requested.lon,
            lat=requested.lat,
            accessible=True,
        )
        for index in range(500)
    ]
    selected = constraints([requested.site_id])
    readiness = assess_readiness(
        bundle.model_copy(update={"candidates": candidates}),
        "site_selection",
        selected,
        FreshnessPolicy(),
    )
    assert readiness.status != "blocked"


def test_api_bounds_alternatives_and_sensitivity_variants():
    base = {
        "question": "Compare cooling center choices",
        "study_area_id": "travis-county",
    }
    alternatives = [
        {"scenario_id": f"scenario-{index}", "constraints": {"num_centers": 1}}
        for index in range(5)
    ]
    with pytest.raises(ValidationError):
        AnalysisCreate.model_validate({**base, "comparison": {"alternatives": alternatives}})
    variants = [
        {"variant_id": f"variant-{index}", "routing_speed_multiplier": 1.1} for index in range(5)
    ]
    with pytest.raises(ValidationError):
        AnalysisCreate.model_validate({**base, "uncertainty": {"variants": variants}})


def test_api_rejects_empty_or_out_of_range_sensitivity_variant():
    base = {
        "question": "Assess uncertainty",
        "study_area_id": "travis-county",
    }
    with pytest.raises(ValidationError):
        AnalysisCreate.model_validate(
            {**base, "uncertainty": {"variants": [{"variant_id": "empty"}]}}
        )
    with pytest.raises(ValidationError):
        AnalysisCreate.model_validate(
            {
                **base,
                "uncertainty": {
                    "variants": [
                        {
                            "variant_id": "too-fast",
                            "routing_speed_multiplier": 2.0,
                        }
                    ]
                },
            }
        )


def test_api_rejects_duplicate_and_reserved_run_ids():
    base = {
        "question": "Compare cooling centers",
        "study_area_id": "travis-county",
    }
    with pytest.raises(ValidationError):
        AnalysisCreate.model_validate(
            {
                **base,
                "comparison": {
                    "alternatives": [
                        {"scenario_id": "same", "constraints": {}},
                        {"scenario_id": "same", "constraints": {}},
                    ]
                },
            }
        )
    with pytest.raises(ValidationError):
        AnalysisCreate.model_validate(
            {
                **base,
                "uncertainty": {
                    "variants": [{"variant_id": "reference", "routing_speed_multiplier": 0.8}]
                },
            }
        )
