"""Baseline-versus-scenario cooling-center analysis built on the deterministic core."""

from __future__ import annotations

import math
from typing import Any

from resilience_map.core.models import AnalysisOverrides, DatasetBundle, ScenarioConstraints
from resilience_map.gis.access import (
    DETOUR_FACTOR,
    SPEED_KMH,
    haversine_km,
    max_distance_km,
)
from resilience_map.gis.routing import RoadRouter, access_matrix
from resilience_map.gis.selection import select_bundle
from resilience_map.optimization.coverage import OptimizationInput, solve
from resilience_map.optimization.eligibility import (
    eligible_candidate_sites,
    usable_baseline_centers,
    vary_candidate_capacities,
)
from resilience_map.risk.scoring import model_weight_provenance, model_with_weight_multipliers
from resilience_map.risk.service import score_dataset, vulnerability_map

ASSUMPTIONS = [
    "Tract population is allocated to the tract centroid; within-tract variation is ignored.",
    f"Travel time is a straight-line proxy (haversine x {DETOUR_FACTOR} detour / assumed speed), not routed "
    "travel. Assumed speeds (km/h): " + ", ".join(f"{m} {v:g}" for m, v in SPEED_KMH.items()) + ".",
    "A tract is 'served' at baseline if its centroid is within the access radius of an existing "
    "center not marked confirmed_closed; unknown opening status remains eligible but is not "
    "confirmation that a center is open. Existing-center capacity is not modelled.",
    "New-site coverage is capacity-limited and counts each resident at most once.",
    "The resilience index is a planning measure, not a validated prediction of health outcomes.",
]
LIMITATIONS = [
    "Candidate-site operating suitability (hours, staffing, accessibility) requires local "
    "human review before any resource allocation.",
    "Results apply only to the pinned dataset version and scoring version shown.",
]


def run_site_selection(
    bundle: DatasetBundle,
    c: ScenarioConstraints,
    max_seconds: float,
    max_candidates: int,
    overrides: AnalysisOverrides | None = None,
) -> dict[str, Any]:
    """Select up to ``c.num_centers`` sites; returns a structured, JSON-serializable result."""
    overrides = overrides or AnalysisOverrides()
    model = model_with_weight_multipliers(overrides.planning_index_weight_multipliers)
    bundle = bundle.model_copy(
        update={
            "candidates": vary_candidate_capacities(
                bundle.candidates, overrides.candidate_capacity_multiplier
            )
        }
    )
    if c.bbox is not None:
        bundle = select_bundle(bundle, c.bbox)
    if c.access_model == "road_network" and bundle.roads is not None:
        RoadRouter(bundle.roads, c.travel_mode)
        west, south, east, north = bundle.roads["coverage_bbox"]
        buffer_km = (
            c.threshold_minutes / 60 * SPEED_KMH[c.travel_mode] * overrides.routing_speed_multiplier
        )
        for tract in bundle.tracts:
            dy = buffer_km / 110.5
            dx = dy / max(0.1, math.cos(math.radians(tract.lat)))
            if not (
                west <= tract.lon - dx
                and tract.lon + dx <= east
                and south <= tract.lat - dy
                and tract.lat + dy <= north
            ):
                raise ValueError(
                    "Road coverage lacks threshold-sized surrounding buffer; prepare a larger snapshot"
                )
    radius = max_distance_km(c.threshold_minutes, c.travel_mode, overrides.routing_speed_multiplier)
    scores = score_dataset(bundle, model)
    vuln = vulnerability_map(scores)

    candidates = eligible_candidate_sites(bundle.candidates, c.candidate_site_ids)
    if len(candidates) > max_candidates:
        raise ValueError(f"Too many candidate sites ({len(candidates)} > {max_candidates})")

    baseline_centers = usable_baseline_centers(bundle.centers)
    baseline, baseline_issues, pin = access_matrix(
        bundle,
        c.travel_mode,
        c.access_model,
        baseline_centers,
        overrides.routing_speed_multiplier,
    )
    dist, candidate_issues, _ = access_matrix(
        bundle,
        c.travel_mode,
        c.access_model,
        candidates,
        overrides.routing_speed_multiplier,
    )
    already = {
        t.tract_id
        for t in bundle.tracts
        if any(baseline[(t.tract_id, e.center_id)] <= c.threshold_minutes for e in baseline_centers)
    }
    opt = solve(
        OptimizationInput(
            tracts=bundle.tracts,
            candidates=candidates,
            vulnerability=vuln,
            already_served=already,
            distance_km=dist,
            radius_km=c.threshold_minutes,
            max_sites=c.num_centers,
            budget_usd=c.budget_usd,
            max_seconds=max_seconds,
        )
    )

    pop = {t.tract_id: t.population for t in bundle.tracts}
    total = sum(pop.values())
    baseline_served = sum(pop[tid] for tid in already)
    site_by_id = {s.site_id: s for s in candidates}
    tract_by_id = {tract.tract_id: tract for tract in bundle.tracts}
    selected = []
    for sid in opt.selected:
        s = site_by_id[sid]
        assigned = opt.assignments.get(sid, {})
        selected.append(
            {
                "site_id": sid,
                "name": s.name,
                "lon": s.lon,
                "lat": s.lat,
                "capacity": s.capacity,
                "cost_usd": s.cost_usd,
                "population_assigned": sum(assigned.values()),
                "tracts_served": [
                    {
                        "tract_id": tid,
                        "population": p,
                        "travel_minutes": round(dist[(tid, sid)], 6),
                        "distance_km": round(
                            haversine_km(
                                tract_by_id[tid].lon,
                                tract_by_id[tid].lat,
                                s.lon,
                                s.lat,
                            ),
                            3,
                        ),
                    }
                    for tid, p in sorted(assigned.items())
                ],
            }
        )
    newly = opt.newly_served_population
    underserved = sorted(
        (
            {
                "tract_id": t.tract_id,
                "name": t.name,
                "population": t.population,
                "score": scores[t.tract_id][1].score,
            }
            for t in bundle.tracts
            if t.tract_id not in already
        ),
        key=lambda r: (
            r["score"] is None,
            r["score"] if r["score"] is not None else 0,
            r["tract_id"],
        ),
    )[:10]
    return {
        "intent": "site_selection",
        "dataset_version": bundle.version,
        "dataset_version_id": bundle.dataset_version_id,
        "scoring_version": bundle.scoring_version,
        "constraints": c.model_dump(),
        "access_model": c.access_model,
        "routing": pin,
        "parameter_overrides": {
            **overrides.model_dump(),
            "effective_planning_index_weights": model_weight_provenance(model),
        },
        "access_issues": baseline_issues + candidate_issues,
        "inventory_status": bundle.inventory_status,
        "inventory": [e.model_dump(mode="json") for e in bundle.centers],
        "recommendation_status": "conditional_human_review"
        if bundle.inventory_status in {"verified", "confirmed_absent"}
        else "exploratory_inventory_incomplete",
        "indicator_readiness": {
            tid: {
                "missing": res.missing,
                "effective_weights": {
                    component.key: component.weight for component in res.components
                },
            }
            for tid, (_, res) in scores.items()
        },
        "access_radius_km": round(radius, 3) if c.access_model == "straight_line_proxy" else None,
        "coverage": {
            "total_population": total,
            "baseline_served": baseline_served,
            "baseline_pct": round(100 * baseline_served / total, 2) if total else 0.0,
            "scenario_served": baseline_served + newly,
            "scenario_pct": round(100 * (baseline_served + newly) / total, 2) if total else 0.0,
            "newly_served": newly,
        },
        "selected_sites": selected,
        "most_vulnerable_underserved_tracts": underserved,
        "solver": {
            "status": opt.status,
            "objective": opt.objective,
            "best_bound": opt.best_bound,
            "runtime_seconds": opt.runtime_seconds,
            "notes": opt.notes,
        },
        "assumptions": [
            ASSUMPTIONS[0],
            (
                ASSUMPTIONS[1]
                if c.access_model == "straight_line_proxy"
                else "Directed mode-specific road routing; endpoint connectors included. No live traffic or heat exposure penalties."
            ),
            f"Sensitivity routing speed multiplier: {overrides.routing_speed_multiplier:g}; candidate capacity multiplier: {overrides.candidate_capacity_multiplier:g}.",
            *ASSUMPTIONS[2:],
        ],
        "limitations": [
            *LIMITATIONS,
            "Empty/unknown inventory does not establish absence. Baseline measures access to listed centers, not confirmed current opening status.",
            "Index and display bands are illustrative; internal consistency checks do not establish real-world accuracy.",
        ],
        "sources": bundle.source_manifest,
    }
