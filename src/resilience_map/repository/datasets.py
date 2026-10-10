"""Read access to published, versioned spatial datasets."""

from __future__ import annotations

import json
import threading
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from resilience_map.core.db import session_scope
from resilience_map.core.errors import NotFoundError
from resilience_map.core.models import (
    CandidateSite,
    CoolingCenter,
    DatasetBundle,
    Indicators,
    Tract,
)
from resilience_map.risk.scoring import BANDS

_IND_COLS = (
    "surface_temp_anomaly_c",
    "pct_age_65_plus",
    "median_income_usd",
    "median_income_moe_usd",
    "canopy_pct",
    "transit_access_index",
)


def list_study_areas(s: Session) -> list[dict[str, Any]]:
    rows = s.execute(
        text("SELECT id, name, description, is_synthetic FROM study_areas ORDER BY id")
    )
    return [dict(r._mapping) for r in rows]


def list_datasets(s: Session, study_area_id: str | None = None) -> list[dict[str, Any]]:
    rows = s.execute(
        text(
            "SELECT id::text, study_area_id, version, scoring_version, published_at, source_manifest, inventory_status "
            "FROM dataset_versions WHERE status = 'published' "
            "AND (CAST(:a AS text) IS NULL OR study_area_id = :a) ORDER BY published_at DESC"
        ),
        {"a": study_area_id},
    )
    return [dict(r._mapping) for r in rows]


def latest_published(s: Session, study_area_id: str) -> dict[str, Any]:
    ds = list_datasets(s, study_area_id)
    if not ds:
        raise NotFoundError(f"No published dataset for study area {study_area_id!r}")
    return ds[0]


def get_published(s: Session, dataset_version_id: str) -> dict[str, Any]:
    row = s.execute(
        text(
            "SELECT id::text, study_area_id, version, scoring_version, published_at, source_manifest, inventory_status "
            "FROM dataset_versions WHERE id = CAST(:i AS uuid) AND status = 'published'"
        ),
        {"i": dataset_version_id},
    ).first()
    if row is None:
        raise NotFoundError("Dataset version not found or not published")
    return dict(row._mapping)


def tracts_geojson(
    s: Session,
    dataset_version_id: str,
    scoring_version: str,
    bbox: tuple[float, float, float, float] | None,
    limit: int,
    county: str | None = None,
) -> dict[str, Any]:
    """Bounded GeoJSON FeatureCollection of tracts with scores and indicators."""
    where = ""
    params: dict[str, Any] = {"dv": dataset_version_id, "sv": scoring_version, "lim": limit + 1}
    if bbox:
        where += " AND t.geom && ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326)"
        params |= dict(zip(("x0", "y0", "x1", "y1"), bbox, strict=True))
    if county:
        where += " AND t.tract_id LIKE :cp"
        params["cp"] = f"{county}%"
    n = s.execute(
        text(
            f"SELECT count(*) FROM tracts t WHERE t.dataset_version_id = CAST(:dv AS uuid) {where}"
        ),
        params,
    ).scalar_one()
    # Draw fewer vertices when many tracts are shown (keeps the payload and the browser fast).
    tolerance = 0.0001 if n <= 600 else 0.0003 if n <= 1500 else 0.0006
    params["tol"] = tolerance
    rows = s.execute(
        text(f"""
            SELECT t.tract_id, t.name, t.population,
                   ST_AsGeoJSON(ST_SimplifyPreserveTopology(t.geom, :tol))::json AS geometry,
                   sc.score, sc.nearest_center_km,
                   i.surface_temp_anomaly_c, i.pct_age_65_plus, i.median_income_usd,
                   i.canopy_pct, i.transit_access_index
            FROM tracts t
            JOIN tract_indicators i USING (dataset_version_id, tract_id)
            LEFT JOIN tract_scores sc ON sc.dataset_version_id = t.dataset_version_id
                 AND sc.tract_id = t.tract_id AND sc.scoring_version = :sv
            WHERE t.dataset_version_id = CAST(:dv AS uuid) {where}
            ORDER BY t.tract_id LIMIT :lim"""),
        params,
    ).all()
    truncated = len(rows) > limit
    feats = [
        {
            "type": "Feature",
            "id": r.tract_id,
            "geometry": r.geometry,
            "properties": {
                "tract_id": r.tract_id,
                "name": r.name,
                "county_id": r.tract_id[:5] if len(r.tract_id) == 11 else None,
                "population": r.population,
                "score": r.score,
                "nearest_center_km": r.nearest_center_km,
                "indicators": {
                    "heat": r.surface_temp_anomaly_c,
                    "age65": r.pct_age_65_plus,
                    "income": r.median_income_usd,
                    "canopy": r.canopy_pct,
                    "transit": r.transit_access_index,
                },
            },
        }
        for r in rows[:limit]
    ]
    return {"type": "FeatureCollection", "features": feats, "truncated": truncated}


def selection_summary(
    s: Session,
    dataset_version_id: str,
    bbox: tuple[float, float, float, float] | None,
    county: str | None,
) -> dict[str, int]:
    """Tract and population count for a county and/or box, using tract centroids.

    Mirrors how analyses and the area assistant select tracts, so the number shown to the user
    is the number that will actually be analysed.
    """
    where = ""
    params: dict[str, Any] = {"dv": dataset_version_id}
    if county:
        where += " AND t.tract_id LIKE :cp"
        params["cp"] = f"{county}%"
    if bbox:
        where += (
            " AND ST_X(t.centroid) BETWEEN :x0 AND :x1 AND ST_Y(t.centroid) BETWEEN :y0 AND :y1"
        )
        params |= {"x0": bbox[0], "y0": bbox[1], "x1": bbox[2], "y1": bbox[3]}
    row = s.execute(
        text(
            "SELECT count(*) AS n, COALESCE(sum(t.population), 0)::bigint AS pop "
            f"FROM tracts t WHERE t.dataset_version_id = CAST(:dv AS uuid){where}"
        ),
        params,
    ).one()
    return {"tracts": int(row.n), "population": int(row.pop)}


def counties_summary(
    s: Session, dataset_version_id: str, scoring_version: str
) -> list[dict[str, Any]]:
    """Per-county rollup (county = first 5 digits of an 11-digit tract GEOID), largest first."""
    rows = s.execute(
        text("""
            SELECT left(t.tract_id, 5) AS county_id, count(*) AS tracts,
                   sum(t.population)::bigint AS population,
                   sum(sc.score * t.population) / NULLIF(sum(t.population) FILTER (WHERE sc.score IS NOT NULL), 0)
                       AS mean_score,
                   count(*) FILTER (WHERE sc.score < :high) AS high_tracts,
                   COALESCE(sum(t.population) FILTER (WHERE sc.score < :high), 0)::bigint AS high_population
            FROM tracts t
            LEFT JOIN tract_scores sc ON sc.dataset_version_id = t.dataset_version_id
                 AND sc.tract_id = t.tract_id AND sc.scoring_version = :sv
            WHERE t.dataset_version_id = CAST(:dv AS uuid) AND t.tract_id ~ '^[0-9]{11}$'
            GROUP BY 1 ORDER BY population DESC"""),
        {"dv": dataset_version_id, "sv": scoring_version, "high": BANDS[0][0]},
    )
    return [dict(r._mapping) for r in rows]


def points(s: Session, dataset_version_id: str, table: str) -> list[dict[str, Any]]:
    if table not in {"cooling_centers", "candidate_sites"}:
        raise ValueError(table)
    cols = (
        "center_id AS id, metadata"
        if table == "cooling_centers"
        else "site_id AS id, cost_usd, accessible"
    )
    rows = s.execute(
        text(
            f"SELECT {cols}, name, capacity, ST_X(geom) AS lon, ST_Y(geom) AS lat "
            f"FROM {table} WHERE dataset_version_id = CAST(:dv AS uuid) ORDER BY 1"
        ),
        {"dv": dataset_version_id},
    )
    return [{**dict(r._mapping), **(r._mapping.get("metadata") or {})} for r in rows]


def tract_detail(
    s: Session, dataset_version_id: str, tract_id: str, scoring_version: str
) -> dict[str, Any]:
    row = s.execute(
        text("""
            SELECT t.tract_id, t.name, t.population, sc.score, sc.components, sc.missing_indicators,
                   sc.nearest_center_km, i.source_dates, i.median_income_moe_usd
            FROM tracts t
            JOIN tract_indicators i USING (dataset_version_id, tract_id)
            LEFT JOIN tract_scores sc ON sc.dataset_version_id = t.dataset_version_id
                 AND sc.tract_id = t.tract_id AND sc.scoring_version = :sv
            WHERE t.dataset_version_id = CAST(:dv AS uuid) AND t.tract_id = :t"""),
        {"dv": dataset_version_id, "t": tract_id, "sv": scoring_version},
    ).first()
    if row is None:
        raise NotFoundError(f"Tract {tract_id} not found in this dataset version")
    return dict(row._mapping)


_cache: dict[str, DatasetBundle] = {}
_cache_lock = threading.Lock()


class PostgresDatasetLoader:
    """Loads (and caches; published versions are immutable) full analysis bundles."""

    def load_bundle(self, dataset_version_id: str) -> DatasetBundle:
        with _cache_lock:
            if dataset_version_id in _cache:
                return _cache[dataset_version_id]
        with session_scope() as s:
            bundle = load_bundle(s, dataset_version_id)
        with _cache_lock:
            if len(_cache) >= 8:
                _cache.pop(next(iter(_cache)))
            _cache[dataset_version_id] = bundle
        return bundle


def load_bundle(
    s: Session, dataset_version_id: str, *, require_published: bool = True
) -> DatasetBundle:
    dv = s.execute(
        text(
            "SELECT id::text, study_area_id, version, scoring_version, status, source_manifest, roads, inventory_status "
            "FROM dataset_versions WHERE id = CAST(:i AS uuid)"
        ),
        {"i": dataset_version_id},
    ).first()
    if dv is None or (require_published and dv.status != "published"):
        raise NotFoundError("Dataset version not found or not published")
    p = {"dv": dataset_version_id}
    tracts = [
        Tract(
            tract_id=r.tract_id,
            name=r.name,
            population=r.population,
            lon=r.lon,
            lat=r.lat,
            indicators=Indicators(**{c: getattr(r, c) for c in _IND_COLS}),
        )
        for r in s.execute(
            text(
                f"SELECT t.tract_id, t.name, t.population, ST_X(t.centroid) AS lon, "
                f"ST_Y(t.centroid) AS lat, {', '.join('i.' + c for c in _IND_COLS)} "
                "FROM tracts t JOIN tract_indicators i USING (dataset_version_id, tract_id) "
                "WHERE t.dataset_version_id = CAST(:dv AS uuid) ORDER BY t.tract_id"
            ),
            p,
        )
    ]
    centers = [
        CoolingCenter(
            center_id=r["id"],
            name=r["name"],
            capacity=r["capacity"],
            lon=r["lon"],
            lat=r["lat"],
            **(r.get("metadata") or {}),
        )
        for r in points(s, dataset_version_id, "cooling_centers")
    ]
    cands = [
        CandidateSite(
            site_id=r["id"],
            name=r["name"],
            capacity=r["capacity"],
            cost_usd=r["cost_usd"],
            lon=r["lon"],
            lat=r["lat"],
            accessible=r["accessible"],
        )
        for r in points(s, dataset_version_id, "candidate_sites")
    ]
    manifest = (
        dv.source_manifest
        if isinstance(dv.source_manifest, list)
        else json.loads(dv.source_manifest)
    )
    return DatasetBundle(
        dataset_version_id=dv.id,
        version=dv.version,
        study_area_id=dv.study_area_id,
        scoring_version=dv.scoring_version,
        tracts=tracts,
        centers=centers,
        candidates=cands,
        source_manifest=manifest,
        roads=dv.roads,
        inventory_status=dv.inventory_status,
    )
