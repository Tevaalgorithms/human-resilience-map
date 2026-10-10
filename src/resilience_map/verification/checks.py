"""Programmatic verification of datasets and analytical outputs.

Checks return a ``VerificationReport``; nothing is published or shown as a recommendation
unless the relevant report ``passed``.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from shapely.geometry import shape

from resilience_map.core.models import AnalysisOverrides, DatasetBundle
from resilience_map.gis.routing import access_matrix
from resilience_map.gis.selection import select_bundle
from resilience_map.optimization.eligibility import (
    eligible_candidate_sites,
    usable_baseline_centers,
    vary_candidate_capacities,
)
from resilience_map.risk.scoring import model_weight_provenance, model_with_weight_multipliers


@dataclass
class VerificationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks_run: int = 0

    @property
    def passed(self) -> bool:
        return not self.errors

    def check(self, ok: bool, message: str, *, warn: bool = False) -> None:
        self.checks_run += 1
        if not ok:
            (self.warnings if warn else self.errors).append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "errors": self.errors,
            "warnings": self.warnings,
            "checks_run": self.checks_run,
        }


def verify_dataset(
    bundle: DatasetBundle,
    geometries: dict[str, dict[str, Any]] | None = None,
    required_indicators: tuple[str, ...] = (
        "surface_temp_anomaly_c",
        "pct_age_65_plus",
        "median_income_usd",
        "canopy_pct",
    ),
) -> VerificationReport:
    """Completeness, duplicates, geometry validity, units and join checks."""
    r = VerificationReport()
    r.check(bool(bundle.tracts), "Dataset has no tracts")
    dupes = [k for k, n in Counter(t.tract_id for t in bundle.tracts).items() if n > 1]
    r.check(not dupes, f"Duplicate tract ids: {dupes[:5]}")
    for name, items, attr in (
        ("cooling center", bundle.centers, "center_id"),
        ("candidate site", bundle.candidates, "site_id"),
    ):
        d = [k for k, n in Counter(getattr(i, attr) for i in items).items() if n > 1]
        r.check(not d, f"Duplicate {name} ids: {d[:5]}")

    for t in bundle.tracts:
        i = t.indicators
        r.check(t.population >= 0, f"Tract {t.tract_id}: negative population")
        r.check(
            -180 <= t.lon <= 180 and -90 <= t.lat <= 90,
            f"Tract {t.tract_id}: centroid outside WGS84 range (check CRS/axis order)",
        )
        if i.pct_age_65_plus is not None:
            r.check(
                0 <= i.pct_age_65_plus <= 100,
                f"Tract {t.tract_id}: pct_age_65_plus {i.pct_age_65_plus} outside 0-100",
            )
        if i.canopy_pct is not None:
            r.check(0 <= i.canopy_pct <= 100, f"Tract {t.tract_id}: canopy_pct outside 0-100")
        if i.transit_access_index is not None:
            r.check(
                0 <= i.transit_access_index <= 1, f"Tract {t.tract_id}: transit index outside 0-1"
            )
        if i.median_income_usd is not None:
            r.check(i.median_income_usd >= 0, f"Tract {t.tract_id}: negative income")
        if i.surface_temp_anomaly_c is not None:
            r.check(
                # Wide on purpose: counties span valley floor to high mountains (anomalies of -20 C are
                # real). This guard exists to catch unit errors (Kelvin ~ +300, Fahrenheit ~ x1.8).
                -40 <= i.surface_temp_anomaly_c <= 40,
                f"Tract {t.tract_id}: temp anomaly {i.surface_temp_anomaly_c} implausible "
                "(units must be degrees C)",
            )
    for key in required_indicators:
        missing = [t.tract_id for t in bundle.tracts if getattr(t.indicators, key) is None]
        frac = len(missing) / max(1, len(bundle.tracts))
        r.check(frac <= 0.1, f"Required indicator {key} missing for {frac:.0%} of tracts")
        r.check(not missing, f"Indicator {key} missing for {len(missing)} tract(s)", warn=True)
    for s in bundle.candidates:
        r.check(s.capacity > 0 and s.cost_usd >= 0, f"Site {s.site_id}: invalid capacity/cost")

    if geometries is not None:
        r.check(
            set(geometries) == {t.tract_id for t in bundle.tracts},
            "Tract geometries do not match tract records (join failure)",
        )
        for tid, geom in geometries.items():
            g = shape(geom)
            r.check(g.is_valid, f"Tract {tid}: invalid geometry")
            r.check(not g.is_empty and g.area > 0, f"Tract {tid}: empty geometry")
    return r


def verify_site_selection(result: dict[str, Any], bundle: DatasetBundle) -> VerificationReport:
    """Constraint, accounting and solver-status checks on a site-selection result."""
    r = VerificationReport()
    c = result["constraints"]
    try:
        overrides = AnalysisOverrides.model_validate(result.get("parameter_overrides") or {})
        model = model_with_weight_multipliers(overrides.planning_index_weight_multipliers)
        r.check(
            (result.get("parameter_overrides") or {}).get("effective_planning_index_weights")
            in (None, model_weight_provenance(model)),
            "Planning-index weight provenance does not match the requested variation",
        )
        bundle = bundle.model_copy(
            update={
                "candidates": vary_candidate_capacities(
                    bundle.candidates, overrides.candidate_capacity_multiplier
                )
            }
        )
    except (ValueError, ValidationError) as exc:
        r.check(False, f"Invalid deterministic parameter override: {exc}")
        overrides = AnalysisOverrides()
    if c.get("bbox") is not None:
        bundle = select_bundle(bundle, tuple(c["bbox"]))
    cov = result["coverage"]
    solver = result["solver"]
    r.check(
        result["access_model"] == c.get("access_model", "straight_line_proxy"),
        "Requested routing model differs from result",
    )
    sites = {s.site_id: s for s in bundle.candidates}
    pops = {t.tract_id: t.population for t in bundle.tracts}

    r.check(
        solver["status"] in {"OPTIMAL", "FEASIBLE", "NO_CANDIDATES"},
        f"Solver status {solver['status']}: no valid recommendation",
    )
    r.check(
        solver["status"] != "FEASIBLE",
        "Solver hit the time limit; solution may be sub-optimal",
        warn=True,
    )
    r.check(len(result["selected_sites"]) <= c["num_centers"], "More sites selected than allowed")
    r.check(sum(pops.values()) == cov["total_population"], "Total population does not reconcile")

    mode = c["travel_mode"]
    model = result["access_model"]
    try:
        candidates = eligible_candidate_sites(bundle.candidates, c.get("candidate_site_ids"))
    except ValueError as exc:
        r.check(False, str(exc))
        candidates = []
    distances, _, pin = access_matrix(
        bundle, mode, model, candidates, overrides.routing_speed_multiplier
    )
    baseline_centers = usable_baseline_centers(bundle.centers)
    baseline, _, _ = access_matrix(
        bundle, mode, model, baseline_centers, overrides.routing_speed_multiplier
    )
    already = {
        t.tract_id
        for t in bundle.tracts
        if any(
            baseline[(t.tract_id, e.center_id)] <= c["threshold_minutes"] for e in baseline_centers
        )
    }
    r.check(result.get("routing") == pin, "Routing pin does not match dataset/assumptions")
    r.check(
        cov["baseline_served"] == sum(pops[tid] for tid in already),
        "Baseline does not reconcile with independently routed access",
    )
    total_cost = 0.0
    assigned_by_tract: Counter[str] = Counter()
    for s in result["selected_sites"]:
        src = sites.get(s["site_id"])
        r.check(src is not None, f"Selected site {s['site_id']} is not a known candidate")
        if src is None:
            continue
        r.check(src.accessible, f"Selected site {s['site_id']} is not marked accessible")
        r.check(
            s["population_assigned"] <= src.capacity,
            f"Site {s['site_id']} exceeds capacity ({s['population_assigned']} > {src.capacity})",
        )
        r.check(
            s["population_assigned"] == sum(ts["population"] for ts in s["tracts_served"]),
            "Site population does not equal its assignments",
        )
        r.check(
            c.get("candidate_site_ids") is None or s["site_id"] in c["candidate_site_ids"],
            "Selected site outside requested candidate list",
        )
        total_cost += src.cost_usd
        for ts in s["tracts_served"]:
            assigned_by_tract[ts["tract_id"]] += ts["population"]
            r.check(
                ts["tract_id"] not in already
                and ts["population"] > 0
                and distances.get((ts["tract_id"], s["site_id"]), math.inf)
                <= c["threshold_minutes"]
                and abs(
                    ts.get("travel_minutes", math.inf)
                    - distances.get((ts["tract_id"], s["site_id"]), math.inf)
                )
                < 1e-5,
                f"Tract {ts['tract_id']} assigned beyond access radius",
            )
    if c.get("budget_usd") is not None:
        r.check(
            total_cost <= c["budget_usd"] + 1e-6,
            f"Budget exceeded ({total_cost} > {c['budget_usd']})",
        )
    for tid, n in assigned_by_tract.items():
        r.check(
            n <= pops.get(tid, 0), f"Tract {tid}: population double counted ({n}>{pops.get(tid)})"
        )

    newly = sum(assigned_by_tract.values())
    r.check(newly == cov["newly_served"], "newly_served does not equal sum of assignments")
    r.check(cov["scenario_served"] == cov["baseline_served"] + newly, "Scenario coverage mismatch")
    r.check(cov["scenario_served"] <= cov["total_population"], "Served population exceeds total")
    r.check(cov["scenario_served"] >= cov["baseline_served"], "Scenario coverage below baseline")
    r.check(
        all(math.isfinite(v) for v in (cov["baseline_pct"], cov["scenario_pct"])),
        "Non-finite coverage percentage",
    )
    return r


def verify_scores(scores: dict[str, float | None]) -> VerificationReport:
    """Score-range check shared by tract-explanation and ranking results."""
    r = VerificationReport()
    for tid, s in scores.items():
        r.check(
            s is None or (math.isfinite(s) and 0 <= s <= 100),
            f"Tract {tid}: score {s} out of range",
        )
    return r
