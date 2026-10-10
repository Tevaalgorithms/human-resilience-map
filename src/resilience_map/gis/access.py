"""Explicit straight-line proxy helpers.

Mode-specific directed road routing is implemented separately in gis.routing. These helpers
remain available only when the caller explicitly selects the labeled straight-line proxy,
or for the existing historical scoring version's disclosed distance indicator.
"""

from __future__ import annotations

from collections.abc import Sequence

from resilience_map.core.geography import EARTH_RADIUS_KM as EARTH_RADIUS_KM
from resilience_map.core.geography import haversine_km as haversine_km
from resilience_map.core.models import TravelMode

DETOUR_FACTOR = 1.3
SPEED_KMH: dict[TravelMode, float] = {"walk": 4.8, "bike": 14.0, "drive": 30.0}


def travel_minutes(distance_km: float, mode: TravelMode, speed_multiplier: float = 1.0) -> float:
    return DETOUR_FACTOR * distance_km / (SPEED_KMH[mode] * speed_multiplier) * 60.0


def max_distance_km(
    threshold_minutes: float, mode: TravelMode, speed_multiplier: float = 1.0
) -> float:
    """Straight-line radius corresponding to a travel-time threshold under the proxy model."""
    return threshold_minutes / 60.0 * SPEED_KMH[mode] * speed_multiplier / DETOUR_FACTOR


def nearest_km(lon: float, lat: float, points: Sequence[tuple[float, float]]) -> float | None:
    if not points:
        return None
    return min(haversine_km(lon, lat, plon, plat) for plon, plat in points)
