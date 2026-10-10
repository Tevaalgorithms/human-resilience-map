from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from resilience_map.backend.auth import current_user
from resilience_map.backend.deps import db_session
from resilience_map.backend.schemas import PreparationIn
from resilience_map.core.config import Settings, get_settings
from resilience_map.core.geography import parse_bbox
from resilience_map.gis.selection import select_bundle
from resilience_map.repository import datasets as repo

router = APIRouter(prefix="/v1", tags=["data"], dependencies=[Depends(current_user)])
Db = Annotated[Session, Depends(db_session)]


@router.get("/study-areas")
def study_areas(db: Db) -> list[dict[str, Any]]:
    return repo.list_study_areas(db)


@router.get("/datasets")
def datasets(db: Db, study_area_id: str | None = None) -> list[dict[str, Any]]:
    return [
        {**d, "published_at": d["published_at"].isoformat()}
        for d in repo.list_datasets(db, study_area_id)
    ]


def _resolve(db: Session, study_area_id: str, dataset_version_id: str | None) -> dict[str, Any]:
    ds = (
        repo.get_published(db, dataset_version_id)
        if dataset_version_id
        else repo.latest_published(db, study_area_id)
    )
    if ds["study_area_id"] != study_area_id:
        raise HTTPException(400, "Dataset version belongs to a different study area")
    return ds


def _county_list(db: Session, ds: dict[str, Any]) -> list[dict[str, Any]]:
    names: dict[str, str] = {}
    for src in ds["source_manifest"]:
        if isinstance(src, dict):
            names.update(src.get("counties", {}))
    return [
        {**c, "name": names.get(c["county_id"], f"County {c['county_id']}")}
        for c in repo.counties_summary(db, ds["id"], ds["scoring_version"])
    ]


@router.get("/counties")
def counties(
    db: Db, study_area_id: str, dataset_version_id: str | None = None
) -> list[dict[str, Any]]:
    """County rollup for the dataset (cheap: no geometry), largest population first."""
    return _county_list(db, _resolve(db, study_area_id, dataset_version_id))


@router.get("/tracts")
def tracts(
    db: Db,
    study_area_id: str,
    dataset_version_id: str | None = None,
    bbox: Annotated[str | None, Query(pattern=r"^-?\d+(\.\d+)?(,-?\d+(\.\d+)?){3}$")] = None,
    county: Annotated[str | None, Query(pattern=r"^\d{5}$")] = None,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> dict[str, Any]:
    """Bounded tract FeatureCollection (scores included) for the map."""
    ds = _resolve(db, study_area_id, dataset_version_id)
    try:
        box = parse_bbox(bbox) if bbox else None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    fc = repo.tracts_geojson(
        db,
        ds["id"],
        ds["scoring_version"],
        box,
        settings.max_map_features,
        county,
    )
    counties = _county_list(db, ds)
    return {
        **fc,
        "counties": counties,
        "dataset": {
            "id": ds["id"],
            "version": ds["version"],
            "scoring_version": ds["scoring_version"],
            "published_at": ds["published_at"].isoformat(),
            "sources": ds["source_manifest"],
        },
    }


@router.get("/tracts/{tract_id}")
def tract(
    db: Db, tract_id: str, study_area_id: str, dataset_version_id: str | None = None
) -> dict[str, Any]:
    ds = _resolve(db, study_area_id, dataset_version_id)
    d = repo.tract_detail(db, ds["id"], tract_id, ds["scoring_version"])
    notices = []
    if d["score"] is None:
        notices.append("Score withheld: too few indicators available for this tract.")
    if d["missing_indicators"]:
        notices.append(f"Missing indicators: {', '.join(d['missing_indicators'])}")
    return {
        **d,
        "dataset_version": ds["version"],
        "scoring_version": ds["scoring_version"],
        "notices": [
            "Index and bands are illustrative planning measures, not validated health predictions.",
            *notices,
        ],
        "effective_weights": {c["key"]: c["weight"] for c in (d["components"] or [])},
    }


@router.get("/cooling-centers")
def centers(
    db: Db, study_area_id: str, dataset_version_id: str | None = None
) -> list[dict[str, Any]]:
    ds = _resolve(db, study_area_id, dataset_version_id)
    return repo.points(db, ds["id"], "cooling_centers")


@router.get("/candidate-sites")
def candidates(
    db: Db, study_area_id: str, dataset_version_id: str | None = None
) -> list[dict[str, Any]]:
    ds = _resolve(db, study_area_id, dataset_version_id)
    return repo.points(db, ds["id"], "candidate_sites")


@router.get("/methodology")
def methodology() -> dict[str, Any]:
    """Scoring model, display bands and standing assumptions, for transparency in the UI."""
    from resilience_map.optimization.scenario import ASSUMPTIONS, LIMITATIONS
    from resilience_map.risk.scoring import BANDS, SCORING_V1

    return {
        "scoring_version": SCORING_V1.version,
        "min_indicators": SCORING_V1.min_indicators,
        "indicators": [
            {
                "key": sp.key,
                "label": sp.label,
                "weight": sp.weight,
                "unit": sp.unit,
                "worst": sp.hi if not sp.higher_is_better else sp.lo,
                "best": sp.lo if not sp.higher_is_better else sp.hi,
            }
            for sp in SCORING_V1.specs
        ],
        "bands": [{"upper": u, "label": n} for u, n in BANDS],
        "assumptions": ASSUMPTIONS,
        "limitations": LIMITATIONS,
    }


@router.get("/readiness")
def readiness(
    db: Db, study_area_id: str, bbox: str, dataset_version_id: str | None = None
) -> dict[str, Any]:
    from resilience_map.risk.service import score_dataset

    ds = _resolve(db, study_area_id, dataset_version_id)
    try:
        selected = select_bundle(repo.load_bundle(db, ds["id"]), parse_bbox(bbox))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "dataset_version_id": ds["id"],
        "bbox": bbox,
        "selection_rule": "tract centroid within box",
        "downloaded_tracts": len(selected.tracts),
        "inventory_status": selected.inventory_status,
        "listed_centers": len(selected.centers),
        "inventory": [center.model_dump(mode="json") for center in selected.centers],
        "inventory_missing_fields": {
            center.center_id: [
                key
                for key in ("hours", "last_confirmed", "capacity", "source", "provenance")
                if not getattr(center, key)
            ]
            for center in selected.centers
        },
        "missing_inputs": (
            ["locally verified cooling inventory"]
            if selected.inventory_status in {"unknown", "partial"}
            else []
        )
        + (
            ["candidate sites with locally verified capacity/cost"]
            if not selected.candidates
            else []
        )
        + (["buffered road snapshot"] if selected.roads is None else []),
        "roads_available": selected.roads is not None,
        "sources": [
            {
                **source,
                "coverage_state": "missing"
                if source.get("kind") == "cooling_inventory"
                and selected.inventory_status == "unknown"
                else "downloaded_snapshot",
                "selected_area_coverage": "inspect source coverage; completeness not established",
            }
            for source in selected.source_manifest
        ],
        "missing_indicators": {
            tid: res.missing for tid, (_, res) in score_dataset(selected).items()
        },
        "discoverable_sources": [
            {
                "name": "OpenStreetMap roads",
                "license": "ODbL",
                "coverage_state": "discoverable_only",
            },
            {
                "name": "NWS monitoring",
                "license": "US government open data",
                "coverage_state": "discoverable_only",
            },
        ],
        "notice": "US demographics/scoring only. Roads and destinations outside selection are retained; road coverage must include a buffer verified by the importer.",
    }


@router.post("/preparations", status_code=202)
def start_preparation(
    body: PreparationIn, user: Annotated[str, Depends(current_user)]
) -> dict[str, str]:
    from resilience_map.backend.preparation import prepare

    ident = prepare(body.state, body.county, user, body.bbox, include_cooling=body.include_cooling)
    return {"id": ident, "status_url": f"/v1/preparations/{ident}"}


@router.get("/preparations/{ident}")
def preparation_status(ident: str, user: Annotated[str, Depends(current_user)]) -> dict[str, Any]:
    from resilience_map.backend.preparation import get_status

    return get_status(ident, user)


@router.get("/preparations/{ident}/download")
def preparation_download(ident: str, user: Annotated[str, Depends(current_user)]) -> Response:
    from resilience_map.backend.preparation import download_sources

    return Response(
        download_sources(ident, user),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="region-{ident}.zip"'},
    )
