"""Result exports (CSV / GeoJSON / plain-text summary) generated from a stored, verified result."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from resilience_map.core.errors import ValidationFailed
from resilience_map.verification.summary import summarize


def _analysis(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("outcome") != "completed" or not result.get("analysis"):
        raise ValidationFailed("Only completed, verified analyses can be exported")
    return dict(result["analysis"])


def to_csv(result: dict[str, Any]) -> str:
    a = _analysis(result)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(
        [f"# dataset={a['dataset_version']} scoring={a['scoring_version']} intent={a['intent']}"]
    )
    if a["intent"] == "site_selection":
        w.writerow(["site_id", "name", "lon", "lat", "capacity", "cost_usd", "population_assigned"])
        for s in a["selected_sites"]:
            w.writerow(
                [
                    s["site_id"],
                    s["name"],
                    s["lon"],
                    s["lat"],
                    s["capacity"],
                    s["cost_usd"],
                    s["population_assigned"],
                ]
            )
    elif a["intent"] == "vulnerability_ranking":
        w.writerow(["rank", "tract_id", "name", "population", "score"])
        for i, r in enumerate(a["ranking"], 1):
            w.writerow([i, r["tract_id"], r["name"], r["population"], r["score"]])
    else:
        w.writerow(["indicator", "raw_value", "unit", "contribution_points"])
        for c in a["tract"]["components"]:
            w.writerow([c["label"], c["raw_value"], c["unit"], c["contribution"]])
    if a["intent"] == "site_selection":
        w.writerow(
            [
                "# analysis_metadata",
                json.dumps(
                    {
                        k: a.get(k)
                        for k in (
                            "access_model",
                            "routing",
                            "inventory",
                            "inventory_status",
                            "access_issues",
                            "assumptions",
                            "limitations",
                            "parameter_overrides",
                        )
                    }
                    | {
                        "readiness": result.get("readiness"),
                        "comparison": result.get("comparison"),
                        "uncertainty": result.get("uncertainty"),
                    }
                ),
            ]
        )
    return _neutralize_formulas(buf.getvalue())


def _neutralize_formulas(csv_text: str) -> str:
    """Guard against spreadsheet formula injection from text fields (e.g. site names)."""
    out = []
    for line in csv_text.splitlines():
        cells = next(csv.reader([line]))
        cells = [
            "'" + c if c[:1] in ("=", "+", "@") or (c[:1] == "-" and not _isnum(c)) else c
            for c in cells
        ]
        b = io.StringIO()
        csv.writer(b, lineterminator="").writerow(cells)
        out.append(b.getvalue())
    return "\n".join(out) + "\n"


def _isnum(c: str) -> bool:
    try:
        float(c)
    except ValueError:
        return False
    return True


def to_geojson(result: dict[str, Any]) -> str:
    a = _analysis(result)
    if a["intent"] != "site_selection":
        raise ValidationFailed("GeoJSON export is available for site-selection analyses")
    feats = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
            "properties": {k: v for k, v in s.items() if k not in ("lon", "lat")},
        }
        for s in a["selected_sites"]
    ]
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": feats,
            "properties": {
                "access_model": a.get("access_model"),
                "routing": a.get("routing"),
                "inventory": a.get("inventory"),
                "inventory_status": a.get("inventory_status"),
                "access_issues": a.get("access_issues"),
                "dataset_version": a["dataset_version"],
                "scoring_version": a["scoring_version"],
                "assumptions": a["assumptions"],
                "limitations": a["limitations"],
                "readiness": result.get("readiness"),
                "comparison": result.get("comparison"),
                "uncertainty": result.get("uncertainty"),
            },
        },
        indent=2,
    )


def to_summary(result: dict[str, Any]) -> str:
    a = _analysis(result)
    parts = [
        summarize(a),
        "Inventory and routing: "
        + json.dumps(
            {k: a.get(k) for k in ("inventory", "inventory_status", "routing", "access_issues")}
        ),
        "Readiness: " + json.dumps(result.get("readiness")),
        "Scenario comparison: " + json.dumps(result.get("comparison")),
        "Sensitivity assessment: " + json.dumps(result.get("uncertainty")),
        "",
        "Assumptions:",
        *[f"- {x}" for x in a.get("assumptions", [])],
        "",
        "Limitations:",
        *[f"- {x}" for x in a.get("limitations", [])],
        "",
        f"Dataset {a['dataset_version']}, {a['scoring_version']}. "
        f"Explanation source: {result.get('explanation_source')}.",
        "AI model: "
        + json.dumps(
            {
                "provider": (result.get("workflow") or {}).get("llm_provider"),
                "model": (result.get("workflow") or {}).get("llm_model"),
            }
        ),
    ]
    return "\n".join(parts) + "\n"
