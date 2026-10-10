from typing import Annotated

from fastapi import APIRouter, Depends, Query

from resilience_map.backend.auth import current_user
from resilience_map.backend.monitoring import (
    DEFAULT_FORECAST_AGE_SECONDS,
    DEFAULT_OBSERVATION_AGE_SECONDS,
    MonitoringSnapshot,
    monitor,
)

router = APIRouter(prefix="/v1", tags=["monitoring"], dependencies=[Depends(current_user)])


@router.get("/monitoring")
def current_monitoring(
    lat: Annotated[float, Query(ge=18, le=72)],
    lon: Annotated[float, Query(ge=-180, le=-60)],
    observation_max_age_seconds: Annotated[
        int, Query(ge=300, le=86400)
    ] = DEFAULT_OBSERVATION_AGE_SECONDS,
    forecast_max_age_seconds: Annotated[
        int, Query(ge=300, le=86400)
    ] = DEFAULT_FORECAST_AGE_SECONDS,
) -> MonitoringSnapshot:
    return monitor.read(lat, lon, observation_max_age_seconds, forecast_max_age_seconds)
