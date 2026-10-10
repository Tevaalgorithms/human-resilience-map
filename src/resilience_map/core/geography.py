"""Geographic primitives and the shared bounded-area validation contract.

This module does not depend on domain models, so request and domain schemas can use the
same validators without circular imports or delaying imports inside model methods.
"""

from __future__ import annotations

import math
from typing import Annotated

from pydantic import AfterValidator

BoundingBox = tuple[float, float, float, float]
EARTH_RADIUS_KM = 6371.0088
MAX_SELECTION_SIDE_KM = 100
MAX_SELECTION_AREA_KM2 = 2500


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def validate_bbox(value: tuple[float, ...]) -> BoundingBox:
    if len(value) != 4 or not all(math.isfinite(v) for v in value):
        raise ValueError("Bounding box requires four finite coordinates")
    west, south, east, north = value
    if not (-180 <= west < east <= -60 and 18 <= south < north <= 72):
        raise ValueError("Select an ordered US bounding box without antimeridian crossing")
    width = haversine_km(west, (north + south) / 2, east, (north + south) / 2)
    height = haversine_km(west, south, west, north)
    if (
        width > MAX_SELECTION_SIDE_KM
        or height > MAX_SELECTION_SIDE_KM
        or width * height > MAX_SELECTION_AREA_KM2
    ):
        raise ValueError("Selection exceeds 100 km side / 2500 square km demonstration limit")
    return west, south, east, north


PlanningBoundingBox = Annotated[BoundingBox, AfterValidator(validate_bbox)]


def parse_bbox(value: str) -> BoundingBox:
    """Parse a request/UI box and apply the exact same bounds as domain validation."""
    return validate_bbox(tuple(float(coordinate) for coordinate in value.split(",")))
