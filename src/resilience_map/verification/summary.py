"""Deterministic plain-text summaries, used when LLM narrative checks fail or LLM is disabled."""

from __future__ import annotations

from typing import Any


def summarize_site_selection(result: dict[str, Any]) -> str:
    cov = result["coverage"]
    c = result["constraints"]
    lines = [
        f"Scenario: up to {c['num_centers']} new cooling center(s), {c['travel_mode']} access "
        f"within {c['threshold_minutes']:g} minutes ({result.get('access_model', 'straight_line_proxy')}).",
        f"Estimated access to listed inventory: {cov['baseline_served']:,} of {cov['total_population']:,} residents "
        f"({cov['baseline_pct']}%) have access to an existing center.",
    ]
    if result["selected_sites"]:
        names = "; ".join(f"{s['name']} ({s['site_id']})" for s in result["selected_sites"])
        lines.append(f"Conditional candidate sites: {names}.")
        lines.append(
            f"Scenario: {cov['scenario_served']:,} residents ({cov['scenario_pct']}%) are served, "
            f"{cov['newly_served']:,} more than baseline."
        )
    else:
        lines.append("No candidate selected; review supplied sites and data readiness.")
    lines.append(f"Solver status: {result['solver']['status']}.")
    lines.append(
        "Inventory status: "
        + result.get("inventory_status", "unknown")
        + ". Empty or unknown inventory does not establish absence; listed hours do not establish current opening."
    )
    lines.append("Candidate sites require local operational review before any allocation.")
    return "\n".join(lines)


def summarize_tract(result: dict[str, Any]) -> str:
    t = result["tract"]
    score = "not available (insufficient data)" if t["score"] is None else f"{t['score']:.1f}/100"
    parts = [
        f"Tract {t['tract_id']} ({t['name']}): resilience score {score} "
        f"(scoring version {result['scoring_version']})."
    ]
    for comp in t["components"]:
        if comp["contribution"] is not None:
            parts.append(
                f"- {comp['label']}: {comp['raw_value']:.2f} {comp['unit']} "
                f"contributes {comp['contribution']:.1f} points."
            )
        else:
            parts.append(f"- {comp['label']}: data unavailable.")
    return "\n".join(parts)


def summarize_ranking(result: dict[str, Any]) -> str:
    rows = result["ranking"]
    lines = [
        f"Most vulnerable tracts (lowest resilience score), dataset {result['dataset_version']}:"
    ]
    lines += [
        f"{i}. {r['tract_id']} ({r['name']}): {r['score']:.1f}, population {r['population']:,}"
        for i, r in enumerate(rows, 1)
    ]
    return "\n".join(lines)


def summarize(result: dict[str, Any]) -> str:
    return {
        "site_selection": summarize_site_selection,
        "tract_explanation": summarize_tract,
        "vulnerability_ranking": summarize_ranking,
    }[result["intent"]](result)
