"""Official California transit enrichment and county package preparation.

Provider calls happen only during explicit operator preparation. Analysis remains offline and pinned
to the resulting files. Cooling records are never promoted to verified status by this module.
"""

from __future__ import annotations

import csv
import json
import math
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from resilience_map.ingestion.census import CANDIDATES_HEADER, CENTERS_HEADER

TRANSIT_URL = (
    "https://caltrans-gis.dot.ca.gov/arcgis/rest/services/CHrailroad/"
    "CA_Transit_Stops/FeatureServer/0/query"
)
LA_COOLING_URL = (
    "https://public.gis.lacounty.gov/public/rest/services/LACounty_Dynamic/"
    "LMS_Data_Public/MapServer/55/query"
)
PAGE_SIZE = 1_000  # LA County layer advertises MaxRecordCount=1000.
MAX_FEATURES = 150_000
MAX_PAGE_BYTES = 20_000_000


def _arcgis_pages(
    url: str,
    fields: str,
    bbox: tuple[float, float, float, float] | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60, connect=10))
    features: list[dict[str, Any]] = []
    offset = 0
    try:
        while True:
            params: dict[str, Any] = {
                "where": "1=1",
                "outFields": fields,
                "returnGeometry": "true",
                "outSR": "4326",
                "f": "geojson",
                "orderByFields": "OBJECTID",
                "resultOffset": offset,
                "resultRecordCount": PAGE_SIZE,
            }
            if bbox is not None:
                params.update(
                    geometry=",".join(map(str, bbox)),
                    geometryType="esriGeometryEnvelope",
                    inSR="4326",
                    spatialRel="esriSpatialRelIntersects",
                )
            response = client.get(url, params=params)
            response.raise_for_status()
            if len(response.content) > MAX_PAGE_BYTES:
                raise ValueError("ArcGIS response page exceeds 20 MB")
            payload = response.json()
            if payload.get("error"):
                raise ValueError(
                    f"ArcGIS provider error: {payload['error'].get('message', 'unknown')}"
                )
            page = payload.get("features")
            if not isinstance(page, list):
                raise ValueError("ArcGIS provider returned invalid GeoJSON")
            features.extend(page)
            if len(features) > MAX_FEATURES:
                raise ValueError("ArcGIS download exceeds the 150,000-feature limit")
            if len(page) < PAGE_SIZE:
                return features
            offset += len(page)
    finally:
        if own:
            client.close()


def _tract_files(directory: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    geojson = json.loads((directory / "tracts.geojson").read_text())
    rows = list(csv.DictReader((directory / "indicators.csv").open()))
    if not rows or "transit_access_index" not in rows[0]:
        raise ValueError("indicators.csv has no transit_access_index column")
    return geojson, rows


def add_california_transit(directory: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """Derive a documented tract index from Caltrans' compiled GTFS stop service."""
    root = Path(directory)
    geojson, rows = _tract_files(root)
    features = geojson.get("features", [])
    geometries = [shape(feature["geometry"]) for feature in features]
    tract_ids = [str(feature["properties"]["tract_id"]) for feature in features]
    bounds = [geometry.bounds for geometry in geometries]
    bbox = (
        min(bound[0] for bound in bounds),
        min(bound[1] for bound in bounds),
        max(bound[2] for bound in bounds),
        max(bound[3] for bound in bounds),
    )
    stops = _arcgis_pages(
        TRANSIT_URL,
        "OBJECTID,agency,stop_id,n_routes,n_arrivals,n_hours_in_service",
        bbox,
        client,
    )
    tree = STRtree(geometries)
    service = [0.0] * len(geometries)
    usable = 0
    outside = 0
    ambiguous = 0
    for stop in stops:
        coordinates = (stop.get("geometry") or {}).get("coordinates")
        properties = stop.get("properties") or {}
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            arrivals = float(properties["n_arrivals"])
            hours = float(properties["n_hours_in_service"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(arrivals) or not math.isfinite(hours) or arrivals < 0 or hours <= 0:
            continue
        matches = list(
            tree.query(Point(float(coordinates[0]), float(coordinates[1])), predicate="intersects")
        )
        if not matches:
            outside += 1
            continue
        if len(matches) > 1:
            ambiguous += 1
        index = min((int(match) for match in matches), key=lambda item: tract_ids[item])
        service[index] += arrivals / hours
        usable += 1

    positive_logs = sorted(math.log1p(value) for value in service if value > 0)
    if not positive_logs:
        raise ValueError("Caltrans returned no usable scheduled service within the tract coverage")
    cap = positive_logs[max(0, math.ceil(0.95 * len(positive_logs)) - 1)]
    index_by_id = {
        tract_id: round(min(1.0, math.log1p(value) / cap), 6) if value > 0 else 0.0
        for tract_id, value in zip(tract_ids, service, strict=True)
    }
    for row in rows:
        row["transit_access_index"] = str(index_by_id[row["tract_id"]])
    with (root / "indicators.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    retrieved = datetime.now(UTC).isoformat(timespec="seconds")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest = [
        entry for entry in manifest if "transit_access_index" not in entry.get("indicators", [])
    ]
    manifest.append(
        {
            "kind": "transit_index",
            "name": "Caltrans compiled GTFS transit stops",
            "url": TRANSIT_URL,
            "license": "Public California government data; verify downstream reuse terms",
            "observation_period": "Provider snapshot at retrieval time",
            "geographic_coverage": "Downloaded tract extent",
            "retrieved_at": retrieved,
            "indicators": ["transit_access_index"],
            "method": (
                "For each tract, sum n_arrivals/n_hours_in_service for Caltrans GTFS stops "
                "inside the tract. Transform with log1p and divide by the statewide/study-area "
                "95th percentile, capped at 1; tracts with no matched scheduled stop receive 0."
            ),
            "quality_flags": [
                "Stop-in-polygon measure does not model walking access across tract boundaries.",
                "Index is relative to this downloaded study area and provider snapshot.",
                "Caltrans aggregation inherits agency GTFS coverage and schedule quality.",
            ],
            "provider_features": len(stops),
            "usable_stop_records": usable,
            "unmatched_stop_records": outside,
            "boundary_ambiguous_stop_records": ambiguous,
            "normalization_cap_log_service": round(cap, 6),
        }
    )
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return {
        "tracts": len(tract_ids),
        "provider_features": len(stops),
        "usable_stop_records": usable,
        "tracts_with_service": sum(value > 0 for value in service),
        "tracts_without_service": sum(value == 0 for value in service),
    }


def create_county_package(source: str, output: str, county_fips: str) -> dict[str, Any]:
    """Create a county-only package without implying that inventories are complete."""
    if len(county_fips) != 5 or not county_fips.isdigit():
        raise ValueError("County FIPS must contain five digits")
    src, out = Path(source), Path(output)
    if out.exists():
        raise ValueError("Output directory already exists")
    geojson, rows = _tract_files(src)
    selected_features = [
        feature
        for feature in geojson["features"]
        if str(feature["properties"]["tract_id"]).startswith(county_fips)
    ]
    selected_ids = {str(feature["properties"]["tract_id"]) for feature in selected_features}
    if not selected_features:
        raise ValueError(f"No tracts found for county {county_fips}")
    out.mkdir(parents=True)
    (out / "tracts.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": selected_features})
    )
    selected_rows = [row for row in rows if row["tract_id"] in selected_ids]
    with (out / "indicators.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(selected_rows)
    (out / "centers.csv").write_text(",".join(CENTERS_HEADER) + "\n")
    (out / "candidates.csv").write_text(",".join(CANDIDATES_HEADER) + "\n")
    manifest = json.loads((src / "manifest.json").read_text())
    manifest.append(
        {
            "kind": "geographic_selection",
            "name": f"County subset {county_fips}",
            "geographic_coverage": f"County FIPS {county_fips}",
            "selection_rule": "Full Census tracts whose GEOID begins with county FIPS",
            "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "indicators": [],
        }
    )
    manifest.append(
        {
            "kind": "cooling_inventory",
            "name": "Locally verified county cooling inventory not supplied",
            "inventory_status": "unknown",
            "geographic_coverage": f"County FIPS {county_fips}; completeness unknown",
            "indicators": [],
        }
    )
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    for optional in ("roads.json",):
        if (src / optional).is_file():
            shutil.copy2(src / optional, out / optional)
    return {"county_fips": county_fips, "tracts": len(selected_features), "directory": str(out)}


def add_la_cooling_inventory(directory: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """Import the official LA layer as partial; zero rows remain explicitly unknown."""
    features = _arcgis_pages(
        LA_COOLING_URL,
        "OBJECTID,source,name,hours,url,description,latitude,longitude,date_updated,isCounty",
        client=client,
    )
    root = Path(directory)
    geojson, _ = _tract_files(root)
    geometries = [shape(feature["geometry"]) for feature in geojson["features"]]
    tree = STRtree(geometries)
    retrieved = datetime.now(UTC).isoformat(timespec="seconds")
    records: list[dict[str, Any]] = []
    skipped = 0
    for feature in features:
        properties = feature.get("properties") or {}
        coordinates = (feature.get("geometry") or {}).get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            lon, lat = properties.get("longitude"), properties.get("latitude")
        else:
            lon, lat = coordinates[:2]
        try:
            lon, lat = float(str(lon)), float(str(lat))
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not len(tree.query(Point(lon, lat), predicate="intersects")):
            skipped += 1
            continue
        object_id = properties.get("OBJECTID")
        records.append(
            {
                "center_id": f"LAC-{object_id}",
                "name": str(properties.get("name") or f"LA County record {object_id}"),
                "capacity": "",
                "lon": lon,
                "lat": lat,
                "hours": properties.get("hours") or "",
                "last_confirmed": "",
                "opening_status": "unknown",
                "source": properties.get("url") or LA_COOLING_URL,
                "provenance": json.dumps(
                    {
                        "provider": "Los Angeles County GIS",
                        "provider_object_id": object_id,
                        "provider_source": properties.get("source"),
                        "provider_date_updated_epoch_ms": properties.get("date_updated"),
                        "retrieved_at": retrieved,
                        "operating_status_confirmed": False,
                    },
                    separators=(",", ":"),
                ),
            }
        )
    with (root / "centers.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CENTERS_HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)

    manifest = json.loads((root / "manifest.json").read_text())
    manifest = [entry for entry in manifest if entry.get("kind") != "cooling_inventory"]
    manifest.append(
        {
            "kind": "cooling_inventory",
            "name": "Los Angeles County GIS cooling-center layer",
            "url": LA_COOLING_URL,
            "license": "License not stated in layer metadata; confirm before redistribution",
            "inventory_status": "partial" if records else "unknown",
            "observation_period": "Provider snapshot at retrieval time",
            "geographic_coverage": "Los Angeles County; completeness not established",
            "retrieved_at": retrieved,
            "verified_by": None,
            "provider_record_count": len(features),
            "imported_record_count": len(records),
            "skipped_record_count": skipped,
            "quality_flags": [
                "Provider does not document statewide or countywide completeness.",
                "Opening status and capacity are not inferred.",
                "A zero-row seasonal layer does not establish confirmed absence.",
                "Local operational review is required before status can be verified.",
            ],
            "indicators": [],
        }
    )
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return {
        "url": LA_COOLING_URL,
        "retrieved_at": retrieved,
        "provider_record_count": len(features),
        "imported_record_count": len(records),
        "coverage_status": "downloaded_partial" if records else "downloaded_empty_unknown",
        "notice": "A zero-row seasonal layer does not establish confirmed absence.",
    }
