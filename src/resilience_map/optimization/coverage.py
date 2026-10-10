"""Constrained cooling-center site selection (OR-Tools CP-SAT).

Model: choose <= k candidate sites. Each tract's population is assigned (at most once) to a
selected site within the access threshold, subject to site capacity. Assignment variables make
double-counting impossible by construction: a person can only be served by one site.
Objective: maximise vulnerability-weighted newly served population.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from resilience_map.core.models import CandidateSite, Tract

_SCALE = 1000  # weights are scaled to integers for CP-SAT


@dataclass(frozen=True)
class OptimizationInput:
    tracts: list[Tract]
    candidates: list[CandidateSite]
    vulnerability: dict[str, float]  # tract_id -> 0..1 (1 = most vulnerable); missing -> 0.5
    already_served: set[str]  # tracts within the radius of an existing center
    distance_km: dict[
        tuple[str, str], float
    ]  # legacy field name: metric matrix; scenario passes minutes
    radius_km: float  # legacy field name: threshold in same units as matrix
    max_sites: int
    budget_usd: float | None
    max_seconds: float = 30.0


@dataclass
class OptimizationResult:
    status: str  # OPTIMAL | FEASIBLE | INFEASIBLE | NO_CANDIDATES | UNKNOWN | MODEL_INVALID
    selected: list[str] = field(default_factory=list)
    assignments: dict[str, dict[str, int]] = field(default_factory=dict)  # site -> tract -> pop
    newly_served_population: int = 0
    objective: float | None = None
    runtime_seconds: float = 0.0
    best_bound: float | None = None
    notes: list[str] = field(default_factory=list)


def solve(inp: OptimizationInput) -> OptimizationResult:
    eligible = [c for c in inp.candidates if c.accessible]
    if not eligible:
        return OptimizationResult(status="NO_CANDIDATES", notes=["No accessible candidate sites."])
    unserved = [t for t in inp.tracts if t.tract_id not in inp.already_served and t.population > 0]
    if not unserved:
        return OptimizationResult(status="OPTIMAL", notes=["All tracts already have access."])

    reach = {
        (t.tract_id, c.site_id)
        for t in unserved
        for c in eligible
        if inp.distance_km[(t.tract_id, c.site_id)] <= inp.radius_km
    }
    m = cp_model.CpModel()
    x = {c.site_id: m.new_bool_var(f"x_{c.site_id}") for c in eligible}
    a: dict[tuple[str, str], cp_model.IntVar] = {}
    pop = {t.tract_id: t.population for t in unserved}
    for tid, sid in sorted(reach):
        a[(tid, sid)] = m.new_int_var(0, pop[tid], f"a_{tid}_{sid}")
        m.add(a[(tid, sid)] <= pop[tid] * x[sid])

    for t in unserved:  # a person is served at most once
        terms = [a[(t.tract_id, c.site_id)] for c in eligible if (t.tract_id, c.site_id) in a]
        if terms:
            m.add(sum(terms) <= t.population)
    for c in eligible:  # capacity
        terms = [a[(t.tract_id, c.site_id)] for t in unserved if (t.tract_id, c.site_id) in a]
        if terms:
            m.add(sum(terms) <= c.capacity)

    m.add(sum(x.values()) <= inp.max_sites)
    if inp.budget_usd is not None:
        m.add(
            sum(math.ceil(c.cost_usd * 100) * x[c.site_id] for c in eligible)
            <= math.floor(inp.budget_usd * 100)
        )

    vuln = {
        t.tract_id: round(_SCALE * (0.5 + inp.vulnerability.get(t.tract_id, 0.5))) for t in unserved
    }
    m.maximize(sum(vuln[tid] * var for (tid, _), var in a.items()))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = inp.max_seconds
    solver.parameters.num_workers = 1  # deterministic
    solver.parameters.random_seed = 0
    status = solver.Solve(m)
    name = solver.StatusName(status)
    res = OptimizationResult(status=name, runtime_seconds=round(solver.WallTime(), 4))
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        res.notes.append("No feasible selection satisfies the constraints.")
        return res

    res.objective = solver.ObjectiveValue() / _SCALE
    res.best_bound = solver.BestObjectiveBound() / _SCALE
    res.selected = sorted(sid for sid, v in x.items() if solver.Value(v))
    for (tid, sid), var in a.items():
        served = solver.Value(var)
        if served > 0 and sid in res.selected:
            res.assignments.setdefault(sid, {})[tid] = served
    res.newly_served_population = sum(sum(d.values()) for d in res.assignments.values())
    if status == cp_model.FEASIBLE:
        res.notes.append("Solver stopped at the time limit; solution may not be optimal.")
    return res
