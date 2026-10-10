"""Authenticated, read-only, dataset-pinned area questions."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from resilience_map.agents.area_assistant import AreaQuestion, answer_area
from resilience_map.agents.llm import get_llm
from resilience_map.backend.auth import current_user
from resilience_map.backend.deps import db_session
from resilience_map.backend.routes.datasets import _resolve
from resilience_map.core.config import Settings, get_settings
from resilience_map.repository import datasets as repo

router = APIRouter(prefix="/v1/assistant", tags=["assistant"], dependencies=[Depends(current_user)])


@router.post("/questions")
def ask(
    body: AreaQuestion,
    db: Annotated[Session, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    if settings.llm_provider == "stub" or not settings.llm_configured:
        raise HTTPException(409, "Configure a live model provider to use the area assistant")
    ds = _resolve(db, body.study_area_id, body.dataset_version_id)
    bundle = repo.load_bundle(db, ds["id"])
    # Release the database transaction before potentially slow provider requests.
    db.rollback()
    return answer_area(body, bundle, get_llm(settings), settings)
