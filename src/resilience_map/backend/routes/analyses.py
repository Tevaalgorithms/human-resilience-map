from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from resilience_map.backend import exports
from resilience_map.backend.auth import current_user
from resilience_map.backend.deps import db_session
from resilience_map.backend.schemas import (
    AnalysisAccepted,
    AnalysisCreate,
    AnalysisStatus,
    Clarification,
    ReviewIn,
)
from resilience_map.core.config import Settings, get_settings
from resilience_map.core.errors import NotFoundError
from resilience_map.repository import datasets as ds_repo
from resilience_map.repository import jobs as jobs_repo

router = APIRouter(prefix="/v1/analyses", tags=["analyses"])
Db = Annotated[Session, Depends(db_session)]
JobId = Annotated[str, Path(pattern=r"^[0-9a-fA-F-]{36}$")]
User = Annotated[str, Depends(current_user)]


def _status(db: Session, job: dict[str, Any]) -> AnalysisStatus:
    result = (
        jobs_repo.get_result(db, job["id"])
        if job["status"] in ("completed", "incomplete")
        else None
    )
    return AnalysisStatus(
        job_id=job["id"],
        status=job["status"],
        progress=job["progress"],
        question=job["question"],
        study_area_id=job["study_area_id"],
        dataset_version_id=job["dataset_version_id"],
        scoring_version=job["scoring_version"],
        clarification=job["clarification"],
        error=job["error"],
        result=result,
        reviews=[
            {**r, "created_at": r["created_at"].isoformat()}
            for r in jobs_repo.list_reviews(db, job["id"])
        ],
        created_at=job["created_at"].isoformat(),
        updated_at=job["updated_at"].isoformat(),
    )


@router.post("", status_code=202, response_model=AnalysisAccepted)
def create_analysis(
    body: AnalysisCreate,
    response: Response,
    db: Db,
    user: User,
    settings: Settings = Depends(get_settings),  # noqa: B008
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> AnalysisAccepted:
    """Validate, pin dataset/scoring versions, persist job + outbox row atomically, return 202."""
    if not settings.llm_configured:
        raise HTTPException(
            409,
            "AI model is not configured. Set the server's model credentials and restart the API and worker before submitting an analysis.",
        )
    ds = (
        ds_repo.get_published(db, body.dataset_version_id)
        if body.dataset_version_id
        else ds_repo.latest_published(db, body.study_area_id)
    )
    if ds["study_area_id"] != body.study_area_id:
        raise NotFoundError("Dataset version not found for this study area")
    job, _created = jobs_repo.create_job(
        db,
        owner_id=user,
        question=body.question,
        request={
            "constraints": body.constraints.model_dump(exclude_none=True),
            "comparison": body.comparison.model_dump(exclude_none=True)
            if body.comparison
            else None,
            "uncertainty": body.uncertainty.model_dump(exclude_none=True)
            if body.uncertainty
            else None,
        },
        study_area_id=body.study_area_id,
        dataset_version_id=ds["id"],
        scoring_version=ds["scoring_version"],
        idempotency_key=idempotency_key,
        max_active=settings.max_jobs_per_user_active,
    )
    url = f"/v1/analyses/{job['id']}"
    response.headers["Location"] = url
    return AnalysisAccepted(job_id=job["id"], status=job["status"], status_url=url)


@router.get("")
def list_analyses(db: Db, user: User) -> list[dict[str, Any]]:
    """The caller's most recent analyses (never anyone else's)."""
    return [{**j, "created_at": j["created_at"].isoformat()} for j in jobs_repo.list_jobs(db, user)]


@router.get("/{job_id}", response_model=AnalysisStatus)
def get_analysis(job_id: JobId, db: Db, user: User) -> AnalysisStatus:
    return _status(db, jobs_repo.get_job(db, job_id, user))


@router.post("/{job_id}/clarifications", status_code=202, response_model=AnalysisAccepted)
def clarify(job_id: JobId, body: Clarification, db: Db, user: User) -> AnalysisAccepted:
    jobs_repo.request_resume(db, job_id, user, body.model_dump(exclude_none=True))
    return AnalysisAccepted(job_id=job_id, status="queued", status_url=f"/v1/analyses/{job_id}")


@router.post("/{job_id}/reviews", status_code=201)
def add_review(job_id: JobId, body: ReviewIn, db: Db, user: User) -> dict[str, str]:
    """Record a planner's operational-suitability review of a recommended site."""
    job = jobs_repo.get_job(db, job_id, user)
    result = jobs_repo.get_result(db, job["id"]) if job["status"] == "completed" else None
    sites = {s["site_id"] for s in ((result or {}).get("analysis") or {}).get("selected_sites", [])}
    if body.site_id not in sites:
        raise NotFoundError("Site is not part of this analysis result")
    jobs_repo.add_review(db, job_id, user, body.site_id, body.decision, body.notes)
    return {"status": "recorded"}


@router.get("/{job_id}/exports")
def export(
    job_id: JobId,
    db: Db,
    user: User,
    format: Annotated[str, Query(pattern="^(csv|geojson|summary)$")] = "summary",
) -> Response:
    job = jobs_repo.get_job(db, job_id, user)  # ownership enforced here
    result = jobs_repo.get_result(db, job["id"])
    if result is None:
        raise NotFoundError("No result available yet")
    body, mime = {
        "csv": (exports.to_csv, "text/csv"),
        "geojson": (exports.to_geojson, "application/geo+json"),
        "summary": (exports.to_summary, "text/plain"),
    }[format]
    ext = {"csv": "csv", "geojson": "geojson", "summary": "txt"}[format]
    return PlainTextResponse(
        body(result),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="analysis-{job_id}.{ext}"'},
    )
