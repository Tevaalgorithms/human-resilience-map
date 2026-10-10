"""Bounded deterministic summaries for explicit scenario alternatives."""

from __future__ import annotations

from typing import Any, Literal

ComparisonObjective = Literal["maximize_newly_served", "maximize_scenario_access", "minimize_cost"]


def scenario_summary(
    scenario_id: str,
    status: str,
    readiness: dict[str, Any],
    analysis: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "scenario_id": scenario_id,
        "status": status,
        "readiness": readiness,
        "verification": verification,
        "reason": reason,
    }
    if analysis is None:
        return row
    selected = analysis["selected_sites"]
    capacity = sum(site["capacity"] for site in selected)
    assigned = sum(site["population_assigned"] for site in selected)
    row.update(
        {
            "constraints": analysis["constraints"],
            "coverage": analysis["coverage"],
            "selected_site_ids": [site["site_id"] for site in selected],
            "cost_usd": sum(site["cost_usd"] for site in selected),
            "capacity": capacity,
            "capacity_used": assigned,
            "capacity_utilization_pct": round(100 * assigned / capacity, 2) if capacity else None,
            "routing": analysis.get("routing"),
            "parameter_overrides": analysis.get("parameter_overrides"),
            "assumptions": analysis.get("assumptions", []),
            "limitations": analysis.get("limitations", []),
        }
    )
    return row


def preferred_scenarios(
    scenarios: list[dict[str, Any]], objective: ComparisonObjective | None
) -> list[str]:
    """Return ties only when the caller explicitly supplied an objective."""
    successful = [row for row in scenarios if row["status"] == "completed"]
    if objective is None or not successful:
        return []
    values = {
        row["scenario_id"]: (
            row["coverage"]["newly_served"]
            if objective == "maximize_newly_served"
            else (
                row["coverage"]["scenario_served"]
                if objective == "maximize_scenario_access"
                else row["cost_usd"]
            )
        )
        for row in successful
    }
    target = min(values.values()) if objective == "minimize_cost" else max(values.values())
    return sorted(scenario_id for scenario_id, value in values.items() if value == target)


def comparison_result(
    dataset_version_id: str,
    scoring_version: str,
    scenarios: list[dict[str, Any]],
    objective: ComparisonObjective | None,
) -> dict[str, Any]:
    return {
        "dataset_version_id": dataset_version_id,
        "scoring_version": scoring_version,
        "objective": objective,
        "preferred_scenario_ids": preferred_scenarios(scenarios, objective),
        "scenarios": scenarios,
        "notice": "Alternatives are compared on the same pinned dataset/scoring version. Failed, blocked, invalid and infeasible alternatives have no benefit value.",
    }
