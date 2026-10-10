from __future__ import annotations

import pytest

from resilience_map.core.models import CandidateSite, Indicators, ScenarioConstraints, Tract
from resilience_map.gis.access import haversine_km, max_distance_km, travel_minutes
from resilience_map.optimization.coverage import OptimizationInput, solve
from resilience_map.optimization.scenario import run_site_selection


def test_haversine_one_degree_latitude():
    assert haversine_km(0, 0, 0, 1) == pytest.approx(111.195, abs=0.01)
    assert haversine_km(10, 20, 10, 20) == 0


def test_radius_and_minutes_are_inverse():
    r = max_distance_km(20, "walk")
    assert travel_minutes(r, "walk") == pytest.approx(20)


def T(tid, pop, lon=0.0):
    return Tract(tract_id=tid, name=tid, population=pop, lon=lon, lat=0.0, indicators=Indicators())


def S(sid, cap, cost=100.0, lon=0.0, acc=True):
    return CandidateSite(
        site_id=sid, name=sid, capacity=cap, cost_usd=cost, lon=lon, lat=0.0, accessible=acc
    )


def inp(tracts, sites, dist, **kw):
    base = dict(  # noqa: C408
        tracts=tracts,
        candidates=sites,
        vulnerability={},
        already_served=set(),
        distance_km=dist,
        radius_km=1.0,
        max_sites=2,
        budget_usd=None,
    )
    return OptimizationInput(**{**base, **kw})


def test_overlapping_coverage_not_double_counted():
    tracts = [T("A", 1000)]
    sites = [S("S1", 5000), S("S2", 5000)]
    dist = {("A", "S1"): 0.5, ("A", "S2"): 0.5}  # both sites cover the same tract
    res = solve(inp(tracts, sites, dist))
    assert res.status == "OPTIMAL"
    assert res.newly_served_population == 1000  # not 2000
    assert sum(a.get("A", 0) for a in res.assignments.values()) == 1000


def test_capacity_limits_assignment():
    tracts = [T("A", 1000)]
    res = solve(inp(tracts, [S("S1", 400)], {("A", "S1"): 0.1}, max_sites=1))
    assert res.newly_served_population == 400


def test_budget_forces_cheaper_site():
    tracts = [T("A", 1000), T("B", 900)]
    sites = [S("S1", 5000, cost=500), S("S2", 5000, cost=100)]
    dist = {("A", "S1"): 0.1, ("A", "S2"): 5.0, ("B", "S1"): 5.0, ("B", "S2"): 0.1}
    res = solve(inp(tracts, sites, dist, max_sites=1, budget_usd=200))
    assert res.selected == ["S2"] and res.newly_served_population == 900


def test_zero_budget_selects_nothing_and_no_candidates_is_explicit():
    tracts = [T("A", 1000)]
    res = solve(inp(tracts, [S("S1", 500, cost=10)], {("A", "S1"): 0.1}, budget_usd=0))
    assert res.selected == [] and res.newly_served_population == 0
    none = solve(inp(tracts, [S("S1", 500, acc=False)], {("A", "S1"): 0.1}))
    assert none.status == "NO_CANDIDATES"


def test_already_served_tracts_excluded():
    tracts = [T("A", 1000), T("B", 500)]
    res = solve(
        inp(
            tracts,
            [S("S1", 5000)],
            {("A", "S1"): 0.1, ("B", "S1"): 0.1},
            already_served={"A"},
            max_sites=1,
        )
    )
    assert res.newly_served_population == 500


def test_solver_is_deterministic(bundle):
    c = ScenarioConstraints(num_centers=3, travel_mode="drive", threshold_minutes=10)
    a = run_site_selection(bundle, c, 10, 50)
    b = run_site_selection(bundle, c, 10, 50)
    assert [s["site_id"] for s in a["selected_sites"]] == [
        s["site_id"] for s in b["selected_sites"]
    ]
    assert a["coverage"] == b["coverage"]


def test_unknown_candidate_rejected(bundle):
    c = ScenarioConstraints(
        num_centers=1, travel_mode="walk", threshold_minutes=10, candidate_site_ids=["NOPE"]
    )
    with pytest.raises(ValueError, match="Unknown candidate"):
        run_site_selection(bundle, c, 5, 50)
