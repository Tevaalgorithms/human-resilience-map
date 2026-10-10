"""Aggregate deterministic sensitivity runs without statistical claims."""

from __future__ import annotations

from collections import Counter
from typing import Any


def sensitivity_summary(
    variant_id: str,
    status: str,
    tested_assumptions: dict[str, Any],
    analysis: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "variant_id": variant_id,
        "status": status,
        "tested_assumptions": tested_assumptions,
        "verification": verification,
        "reason": reason,
    }
    if analysis is not None:
        row.update(
            {
                "coverage": analysis["coverage"],
                "cost_usd": sum(site["cost_usd"] for site in analysis["selected_sites"]),
                "selected_site_ids": [site["site_id"] for site in analysis["selected_sites"]],
                "routing": analysis.get("routing"),
            }
        )
    return row


def uncertainty_result(
    dataset_version_id: str,
    scoring_version: str,
    runs: list[dict[str, Any]],
    unsupported_parameters: list[str],
) -> dict[str, Any]:
    successful = [run for run in runs if run["status"] == "completed"]
    coverage = [run["coverage"]["newly_served"] for run in successful]
    costs = [run["cost_usd"] for run in successful]
    selections = [tuple(run["selected_site_ids"]) for run in successful]
    reference_sites = set(successful[0]["selected_site_ids"]) if successful else set()
    frequencies = Counter(site for run in successful for site in run["selected_site_ids"])
    return {
        "dataset_version_id": dataset_version_id,
        "scoring_version": scoring_version,
        "runs": runs,
        "tested_run_count": len(successful),
        "coverage_range": [min(coverage), max(coverage)] if coverage else None,
        "cost_range_usd": [min(costs), max(costs)] if costs else None,
        "site_selection_frequency": {
            site: {"selected_runs": count, "successful_runs": len(successful)}
            for site, count in sorted(frequencies.items())
        },
        "selected_site_changes": {
            run["variant_id"]: {
                "added": sorted(set(run["selected_site_ids"]) - reference_sites),
                "removed": sorted(reference_sites - set(run["selected_site_ids"])),
            }
            for run in successful
        },
        "stable_recommendation": len(successful) >= 2 and len(set(selections)) == 1,
        "failed_runs": [run for run in runs if run["status"] != "completed"],
        "unsupported_parameters": sorted(set(unsupported_parameters)),
        "limitations": [
            "These are bounded sensitivity results, not confidence intervals, probabilities or health-outcome predictions.",
            "Missing facilities and missing indicator values are not imputed.",
            "Stability applies only to the explicitly tested parameter values and pinned data.",
        ],
    }
