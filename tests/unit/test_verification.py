from __future__ import annotations

import copy

from resilience_map.core.models import ScenarioConstraints
from resilience_map.optimization.scenario import run_site_selection
from resilience_map.verification.checks import verify_dataset, verify_site_selection
from resilience_map.verification.narrative import check_narrative
from resilience_map.verification.summary import summarize


def good_result(bundle):
    c = ScenarioConstraints(
        num_centers=2, travel_mode="drive", threshold_minutes=8, budget_usd=700_000
    )
    return run_site_selection(bundle, c, 10, 50)


def test_synthetic_dataset_passes(synthetic):
    bundle, geoms = synthetic
    report = verify_dataset(bundle, geoms)
    assert report.passed, report.errors


def test_dataset_checks_catch_bad_units_and_duplicates(bundle):
    t0 = bundle.tracts[0]
    bad = t0.model_copy(
        update={
            "indicators": t0.indicators.model_copy(
                update={"surface_temp_anomaly_c": 300.0, "pct_age_65_plus": 140.0}
            )
        }
    )
    broken = bundle.model_copy(update={"tracts": [bad, bad, *bundle.tracts[1:]]})
    errors = " ".join(verify_dataset(broken).errors)
    assert "Duplicate tract ids" in errors and "implausible" in errors and "outside 0-100" in errors


def test_dataset_geometry_mismatch_detected(synthetic):
    bundle, geoms = synthetic
    missing = dict(list(geoms.items())[1:])
    assert not verify_dataset(bundle, missing).passed


def test_valid_result_passes(bundle):
    assert verify_site_selection(good_result(bundle), bundle).passed


def test_tampered_results_fail(bundle):
    r = good_result(bundle)
    over_budget = copy.deepcopy(r)
    over_budget["constraints"]["budget_usd"] = 1.0
    assert not verify_site_selection(over_budget, bundle).passed

    dbl = copy.deepcopy(r)
    dbl["selected_sites"][0]["tracts_served"].append(
        dict(dbl["selected_sites"][0]["tracts_served"][0])
    )
    dbl["selected_sites"][0]["population_assigned"] *= 2
    assert not verify_site_selection(dbl, bundle).passed

    bad_status = copy.deepcopy(r)
    bad_status["solver"]["status"] = "INFEASIBLE"
    assert not verify_site_selection(bad_status, bundle).passed


def test_narrative_accepts_grounded_and_rejects_invented(bundle):
    r = good_result(bundle)
    assert check_narrative(summarize(r), r).ok
    fake = summarize(r) + " This will save 12345 lives at SITE-99."
    chk = check_narrative(fake, r)
    assert not chk.ok and "12345" in chk.unsupported_numbers and "SITE-99" in chk.unsupported_ids
