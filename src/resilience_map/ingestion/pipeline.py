"""Ingestion pipeline: snapshot -> parse/normalize -> validate -> load -> score -> publish.

Guarantees:
* raw snapshots are stored immutably (content-addressed) before anything else happens;
* the dataset version id derives from the snapshot hash, so re-ingesting identical data is a no-op;
* a version becomes ``published`` only after every verification check passes;
* a failed refresh is recorded as ``failed`` and the previously published version stays current.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from dataclasses import dataclass
from typing import Any

from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from sqlalchemy import text
from sqlalchemy.orm import Session

from resilience_map.core.db import session_scope
from resilience_map.core.models import (
    CandidateSite,
    CoolingCenter,
    DatasetBundle,
    Indicators,
    Tract,
)
from resilience_map.core.object_store import ObjectStore
from resilience_map.ingestion.sources import REQUIRED_FILES, Snapshot, SourceAdapter
from resilience_map.risk.scoring import SCORING_VERSION
from resilience_map.risk.service import score_dataset
from resilience_map.verification.checks import verify_dataset

log = logging.getLogger(__name__)
_IND = (
    "surface_temp_anomaly_c",
    "pct_age_65_plus",
    "median_income_usd",
    "median_income_moe_usd",
    "canopy_pct",
    "transit_access_index",
)


@dataclass
class IngestResult:
    status: str  # published | unchanged | failed
    dataset_version_id: str | None
    version: str
    report: dict[str, Any]


def snapshot_hash(snap: Snapshot) -> str:
    h = hashlib.sha256()
    h.update(b"hrm-ingestion-schema-v2")
    for name in sorted(snap.files):
        h.update(name.encode())
        h.update(hashlib.sha256(snap.files[name]).digest())
    return h.hexdigest()


def _f(v: str | None) -> float | None:
    return None if v is None or v.strip() == "" else float(v)


def _normalize_geom(geom: dict[str, Any], tract_id: str) -> MultiPolygon:
    g = shape(geom)
    if not g.is_valid:
        g = make_valid(g)
    polys: list[Polygon] = []
    if isinstance(g, Polygon):
        polys = [g]
    elif isinstance(g, MultiPolygon):
        polys = list(g.geoms)
    else:  # GeometryCollection after make_valid: keep polygonal parts only
        polys = [
            p
            for part in getattr(g, "geoms", [])
            if isinstance(part, (Polygon, MultiPolygon))
            for p in (part.geoms if isinstance(part, MultiPolygon) else [part])
        ]
    if not polys:
        raise ValueError(f"Tract {tract_id}: no polygonal geometry")
    return MultiPolygon(polys)


def parse_snapshot(files: dict[str, bytes]) -> tuple[DatasetBundle, dict[str, dict[str, Any]]]:
    """Parse + normalize. Returns a (version-less) bundle and ``tract_id -> geometry``."""
    if len(files.get("roads.json", b"")) > 20_000_000:
        raise ValueError("Road snapshot exceeds 20 MB import limit")
    manifest: list[dict[str, Any]] = json.loads(files["manifest.json"])
    gj = json.loads(files["tracts.geojson"])
    crs = (gj.get("crs") or {}).get("properties", {}).get("name", "")
    if crs and not any(k in crs for k in ("CRS84", "4326")):
        raise ValueError(f"Unsupported CRS {crs!r}: reproject to EPSG:4326 before ingestion")
    ind_rows = {
        r["tract_id"].strip(): r
        for r in csv.DictReader(io.StringIO(files["indicators.csv"].decode()))
    }

    tracts, geoms = [], {}
    for feat in gj["features"]:
        p = feat["properties"]
        tid = str(p["tract_id"]).strip()
        mp = _normalize_geom(feat["geometry"], tid)
        geoms[tid] = mapping(mp)
        c = mp.centroid
        row = ind_rows.get(tid, {})
        tracts.append(
            Tract(
                tract_id=tid,
                name=str(p.get("name") or tid),
                population=int(p["population"]),
                lon=c.x,
                lat=c.y,
                indicators=Indicators(**{k: _f(row.get(k)) for k in _IND}),
            )
        )
    centers = [
        CoolingCenter(
            center_id=r["center_id"].strip(),
            name=r["name"],
            capacity=int(r["capacity"]) if r.get("capacity") else None,
            lon=float(r["lon"]),
            lat=float(r["lat"]),
            hours=r.get("hours") or None,
            last_confirmed=r.get("last_confirmed") or None,
            source=r.get("source") or None,
            provenance=json.loads(r.get("provenance") or "{}"),
            opening_status=r.get("opening_status") or "unknown",
        )
        for r in csv.DictReader(io.StringIO(files["centers.csv"].decode()))
    ]
    cands = [
        CandidateSite(
            site_id=r["site_id"].strip(),
            name=r["name"],
            capacity=int(r["capacity"]),
            cost_usd=float(r["cost_usd"]),
            lon=float(r["lon"]),
            lat=float(r["lat"]),
            accessible=r["accessible"].strip().lower() in {"true", "1", "yes"},
        )
        for r in csv.DictReader(io.StringIO(files["candidates.csv"].decode()))
    ]
    bundle = DatasetBundle(
        dataset_version_id="",
        version="",
        study_area_id="",
        scoring_version=SCORING_VERSION,
        tracts=sorted(tracts, key=lambda t: t.tract_id),
        centers=centers,
        candidates=cands,
        source_manifest=manifest,
        roads=json.loads(files["roads.json"]) if "roads.json" in files else None,
        inventory_status=next(
            (
                s.get("inventory_status", "unknown")
                for s in manifest
                if s.get("kind") == "cooling_inventory"
            ),
            "unknown",
        ),
    )
    from resilience_map.ingestion.inventory import validate_inventory

    validate_inventory(bundle)
    if bundle.roads is not None:
        from resilience_map.gis.routing import RoadRouter

        RoadRouter(bundle.roads, next(iter(bundle.roads["edges"][0]["minutes"])))
    return bundle, geoms


def run_ingestion(adapter: SourceAdapter, store: ObjectStore) -> IngestResult:
    snap = adapter.fetch()
    missing = [f for f in REQUIRED_FILES if f not in snap.files]
    if missing:
        raise ValueError(f"Snapshot missing files: {missing}")
    digest = snapshot_hash(snap)
    version = f"v-{digest[:12]}"

    # 1. immutable raw snapshot (before any transformation)
    checksums = {}
    for name, data in snap.files.items():
        key = f"raw/{snap.study_area_id}/{digest}/{name}"
        checksums[name] = (
            store.put(key, data) if not store.exists(key) else hashlib.sha256(data).hexdigest()
        )

    with session_scope() as s:
        s.execute(
            text(
                "INSERT INTO study_areas (id, name, is_synthetic) VALUES (:i, :n, :syn) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name"
            ),
            {"i": snap.study_area_id, "n": snap.study_area_name, "syn": snap.is_synthetic},
        )
        existing = s.execute(
            text(
                "SELECT id::text, status FROM dataset_versions WHERE study_area_id = :a AND version = :v"
            ),
            {"a": snap.study_area_id, "v": version},
        ).first()
        if existing and existing.status == "published":
            return IngestResult(
                "unchanged", existing.id, version, {"passed": True, "note": "identical snapshot"}
            )
        if existing:  # previous failed/building attempt: remove and retry from the same snapshot
            s.execute(
                text("DELETE FROM dataset_versions WHERE id = CAST(:i AS uuid)"), {"i": existing.id}
            )

    # 2. parse + normalize + validate (in memory, nothing visible yet)
    try:
        bundle, geoms = parse_snapshot(snap.files)
        report = verify_dataset(bundle, geoms)
    except Exception as e:
        log.exception("ingestion parse failed")
        return _record_failure(snap, version, checksums, {"passed": False, "errors": [str(e)]})
    if not report.passed:
        return _record_failure(snap, version, checksums, report.to_dict())

    # 3. load + score + publish atomically
    manifest = [
        {**m, "snapshot_sha256": digest, "acquired_at": snap.acquired_at, "checksums": checksums}
        if i == 0
        else m
        for i, m in enumerate(bundle.source_manifest)
    ]
    with session_scope() as s:
        dv_id = _load(s, snap, version, bundle, geoms, manifest, report.to_dict())
    log.info("published dataset %s (%s)", version, dv_id)
    return IngestResult("published", dv_id, version, report.to_dict())


def _record_failure(
    snap: Snapshot, version: str, checksums: dict[str, str], report: dict[str, Any]
) -> IngestResult:
    with session_scope() as s:
        s.execute(
            text(
                "INSERT INTO dataset_versions (study_area_id, version, status, scoring_version, "
                "source_manifest, validation_report) VALUES (:a, :v, 'failed', :sv, CAST(:m AS jsonb), "
                "CAST(:r AS jsonb)) ON CONFLICT (study_area_id, version) DO NOTHING"
            ),
            {
                "a": snap.study_area_id,
                "v": version,
                "sv": SCORING_VERSION,
                "m": json.dumps([{"checksums": checksums}]),
                "r": json.dumps(report),
            },
        )
    log.error(
        "ingestion failed validation; last published version remains current: %s",
        report.get("errors"),
    )
    return IngestResult("failed", None, version, report)


def _load(
    s: Session,
    snap: Snapshot,
    version: str,
    b: DatasetBundle,
    geoms: dict[str, dict[str, Any]],
    manifest: list[dict[str, Any]],
    report: dict[str, Any],
) -> str:
    dv_id = s.execute(
        text(
            "INSERT INTO dataset_versions (study_area_id, version, status, scoring_version, source_manifest, "
            "validation_report) VALUES (:a, :v, 'building', :sv, CAST(:m AS jsonb), CAST(:r AS jsonb)) "
            "RETURNING id::text"
        ),
        {
            "a": snap.study_area_id,
            "v": version,
            "sv": SCORING_VERSION,
            "m": json.dumps(manifest),
            "r": json.dumps(report),
        },
    ).scalar_one()
    dates: dict[str, object] = {}
    for m in b.source_manifest:
        indicators = m.get("indicators")
        if isinstance(indicators, list):
            for ind in indicators:
                dates[str(ind)] = m.get("observation_period")
    for t in b.tracts:
        s.execute(
            text(
                "INSERT INTO tracts (dataset_version_id, tract_id, name, population, geom, centroid) VALUES "
                "(CAST(:d AS uuid), :t, :n, :p, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326)), "
                "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))"
            ),
            {
                "d": dv_id,
                "t": t.tract_id,
                "n": t.name,
                "p": t.population,
                "g": json.dumps(geoms[t.tract_id]),
                "lon": t.lon,
                "lat": t.lat,
            },
        )
        s.execute(
            text(
                f"INSERT INTO tract_indicators (dataset_version_id, tract_id, {', '.join(_IND)}, source_dates) "
                f"VALUES (CAST(:d AS uuid), :t, {', '.join(':' + k for k in _IND)}, CAST(:sd AS jsonb))"
            ),
            {"d": dv_id, "t": t.tract_id, **t.indicators.model_dump(), "sd": json.dumps(dates)},
        )
    s.execute(
        text(
            "UPDATE dataset_versions SET roads=CAST(:roads AS jsonb), inventory_status=:status WHERE id=CAST(:d AS uuid)"
        ),
        {"d": dv_id, "roads": json.dumps(b.roads), "status": b.inventory_status},
    )
    for c in b.centers:
        s.execute(
            text(
                "INSERT INTO cooling_centers (dataset_version_id, center_id, name, capacity, metadata, geom) VALUES "
                "(CAST(:d AS uuid), :i, :n, :c, CAST(:metadata AS jsonb), ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))"
            ),
            {
                "d": dv_id,
                "i": c.center_id,
                "metadata": json.dumps(
                    c.model_dump(
                        mode="json", exclude={"center_id", "name", "capacity", "lon", "lat"}
                    )
                ),
                "n": c.name,
                "c": c.capacity,
                "lon": c.lon,
                "lat": c.lat,
            },
        )
    for site in b.candidates:
        s.execute(
            text(
                "INSERT INTO candidate_sites (dataset_version_id, site_id, name, capacity, cost_usd, "
                "accessible, geom) VALUES (CAST(:d AS uuid), :i, :n, :c, :k, :a, "
                "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))"
            ),
            {
                "d": dv_id,
                "i": site.site_id,
                "n": site.name,
                "c": site.capacity,
                "k": site.cost_usd,
                "a": site.accessible,
                "lon": site.lon,
                "lat": site.lat,
            },
        )
    for tid, (km, res) in score_dataset(b).items():
        s.execute(
            text(
                "INSERT INTO tract_scores (dataset_version_id, tract_id, scoring_version, score, components, "
                "missing_indicators, nearest_center_km) VALUES (CAST(:d AS uuid), :t, :sv, :s, "
                "CAST(:c AS jsonb), CAST(:m AS jsonb), :km)"
            ),
            {
                "d": dv_id,
                "t": tid,
                "sv": res.scoring_version,
                "s": res.score,
                "km": km,
                "c": json.dumps([c.__dict__ for c in res.components]),
                "m": json.dumps(res.missing),
            },
        )
    # Publish last, inside the same transaction as the data, after verification passed.
    s.execute(
        text(
            "UPDATE dataset_versions SET status = 'published', published_at = now() "
            "WHERE id = CAST(:d AS uuid)"
        ),
        {"d": dv_id},
    )
    return str(dv_id)
