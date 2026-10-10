"""Raw CSV/ZIP upload API, with separate validation and publication steps."""

from __future__ import annotations

import io
import zipfile
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from resilience_map.backend import imports
from resilience_map.backend.auth import current_user
from resilience_map.backend.routes.datasets import Db, _resolve
from resilience_map.core.db import session_scope
from resilience_map.core.object_store import get_object_store
from resilience_map.ingestion.census import CENTERS_HEADER
from resilience_map.repository import datasets as repo

router = APIRouter(prefix="/v1", tags=["data import"], dependencies=[Depends(current_user)])
User = Annotated[str, Depends(current_user)]
Area = Annotated[str, Query(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")]


@router.get("/cooling-centers/template")
def cooling_template() -> Response:
    return Response(
        ",".join(CENTERS_HEADER) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="centers.csv"'},
    )


@router.post("/imports", status_code=201)
async def upload(
    request: Request,
    user: User,
    study_area_id: Area,
    kind: Literal["cooling", "region"],
    study_area_name: Annotated[str, Query(max_length=200)] = "",
    dataset_version_id: str | None = None,
    source: Annotated[str, Query(max_length=2000)] = "",
) -> dict[str, Any]:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > imports.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Upload exceeds 20 MB")
        data.extend(chunk)
    if not data:
        raise HTTPException(422, "Upload is empty")

    def validate() -> dict[str, Any]:
        try:
            name = study_area_name or study_area_id
            if kind == "cooling":
                # Resolve the pinned baseline inside the worker's own DB session.
                with session_scope() as db:
                    dataset = _resolve(db, study_area_id, dataset_version_id)
                    areas = repo.list_study_areas(db)
                    name = next(a["name"] for a in areas if a["id"] == study_area_id)
                files = imports.replace_cooling(
                    imports.source_files(dataset, get_object_store()), bytes(data), source
                )
            else:
                files = imports.unpack(bytes(data))
            return imports.stage(files, study_area_id, name, user)
        except (ValueError, KeyError, TypeError, AttributeError, OSError, OverflowError) as exc:
            raise HTTPException(422, f"Invalid source data: {exc}") from exc

    return await run_in_threadpool(validate)


@router.get("/imports/{ident}")
def import_status(ident: str, user: User) -> dict[str, Any]:
    return imports.get_status(ident, user)


@router.post("/imports/{ident}/publish")
def publish_import(ident: str, user: User) -> dict[str, Any]:
    return imports.publish(ident, user, get_object_store())


@router.get("/datasets/{ident}/download")
def dataset_download(ident: str, db: Db) -> Response:
    dataset = repo.get_published(db, ident)
    try:
        files = imports.source_files(dataset, get_object_store())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return Response(
        output.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="region-sources.zip"',
        },
    )
