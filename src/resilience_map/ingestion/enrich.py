"""Add real heat and canopy indicators to a source directory produced by ``prep-census``."""

from __future__ import annotations

import csv
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shapely.geometry import shape

from resilience_map.ingestion.canopy import compute_canopy, wcs_tile_fetcher
from resilience_map.ingestion.heat import anomalies, compute_heat, county_groups

log = logging.getLogger(__name__)
PUBLIC = "US Government work (public domain)"


def _progress(label: str) -> Any:
    def p(done: int, total: int) -> None:
        if done == total or done % max(1, total // 10) == 0:
            log.info("%s: %d/%d", label, done, total)

    return p


def enrich_directory(
    directory: str,
    years: list[int],
    scenes_per_tile: int = 8,
    do_heat: bool = True,
    do_canopy: bool = True,
    canopy_year: int = 2021,
    decimate: int = 1,
    max_cloud: float = 15.0,
) -> dict[str, Any]:
    d = Path(directory)
    gj = json.loads((d / "tracts.geojson").read_text())
    ids = [f["properties"]["tract_id"] for f in gj["features"]]
    geoms = [shape(f["geometry"]) for f in gj["features"]]
    rows = list(csv.DictReader((d / "indicators.csv").open()))
    by_id = {r["tract_id"]: r for r in rows}
    manifest: list[dict[str, Any]] = json.loads((d / "manifest.json").read_text())
    now = datetime.now(UTC).isoformat(timespec="seconds")
    summary: dict[str, Any] = {"tracts": len(ids)}

    if do_canopy:
        canopy = compute_canopy(
            geoms, wcs_tile_fetcher(canopy_year), progress=_progress("canopy tiles")
        )
        for tid, v in zip(ids, canopy, strict=True):
            by_id[tid]["canopy_pct"] = "" if v is None else v
        manifest = [m for m in manifest if "canopy_pct" not in m.get("indicators", [])]
        manifest.append({
            "name": f"NLCD/USFS Tree Canopy Cover CONUS v2021-4 ({canopy_year})",
            "url": f"https://www.mrlc.gov/data (WCS: mrlc_download__nlcd_tcc_conus_{canopy_year}_v2021-4)",
            "license": PUBLIC, "observation_period": str(canopy_year),
            "geographic_coverage": "CONUS", "indicators": ["canopy_pct"], "retrieved_at": now,
            "method": "mean of valid 30 m pixels (0-100) per tract",
        })  # fmt: skip
        summary["canopy_missing"] = sum(1 for v in canopy if v is None)

    if do_heat:
        res = compute_heat(
            geoms,
            years,
            scenes_per_tile=scenes_per_tile,
            progress=_progress("landsat scenes"),
            decimate=decimate,
            max_cloud=max_cloud,
        )
        anom = anomalies(res.lst_c, county_groups(ids))
        for tid, a in zip(ids, anom, strict=True):
            by_id[tid]["surface_temp_anomaly_c"] = "" if a is None else a
        with (d / "heat_details.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["tract_id", "lst_median_c", "anomaly_c", "scenes_used"])
            for tid, lst, a, n in zip(ids, res.lst_c, anom, res.scene_counts, strict=True):
                w.writerow([tid, "" if lst is None else lst, "" if a is None else a, n])
        manifest = [m for m in manifest if "surface_temp_anomaly_c" not in m.get("indicators", [])]
        manifest.append({
            "name": "Landsat 8/9 Collection 2 Level-2 surface temperature (ST_B10)",
            "url": "https://planetarycomputer.microsoft.com/dataset/landsat-c2-l2",
            "license": PUBLIC, "observation_period": f"Jun 1-Sep 15 of {', '.join(map(str, years))}",
            "geographic_coverage": "study area", "indicators": ["surface_temp_anomaly_c"],
            "retrieved_at": now, "scenes": res.scenes_used,
            "method": "daytime LST, QA-masked (cloud/shadow/snow/water), per-tract median across scenes, "
                      "anomaly = tract LST minus the median tract LST of its county; SURFACE not air temperature",
        })  # fmt: skip
        summary |= {
            "heat_missing": sum(1 for a in anom if a is None),
            "scenes_used": len(res.scenes_used),
        }

    with (d / "indicators.csv").open("w", newline="") as fh:
        w2 = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        w2.writeheader()
        w2.writerows(rows)
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return summary


def recompute_anomalies(directory: str) -> int:
    """Re-derive anomalies from the saved per-tract LST (no re-download). Returns tracts updated."""
    d = Path(directory)
    details = list(csv.DictReader((d / "heat_details.csv").open()))
    ids = [r["tract_id"] for r in details]
    lst = [float(r["lst_median_c"]) if r["lst_median_c"] else None for r in details]
    anom = anomalies(lst, county_groups(ids))
    new = {tid: ("" if a is None else a) for tid, a in zip(ids, anom, strict=True)}
    for r, a in zip(details, anom, strict=True):
        r["anomaly_c"] = "" if a is None else a
    rows = list(csv.DictReader((d / "indicators.csv").open()))
    for r in rows:
        r["surface_temp_anomaly_c"] = new.get(r["tract_id"], "")
    with (d / "indicators.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    with (d / "heat_details.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(details[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(details)
    manifest = json.loads((d / "manifest.json").read_text())
    for m in manifest:
        if "surface_temp_anomaly_c" in m.get("indicators", []):
            m["method"] = (
                "daytime LST, QA-masked (cloud/shadow/snow/water), per-tract median across scenes, "
                "anomaly = tract LST minus the median tract LST of its county; SURFACE not air temperature"
            )
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return len(ids)
