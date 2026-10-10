"""Bounded US planning selection; destinations and roads remain buffered/full snapshot."""

from resilience_map.core.geography import validate_bbox as validate_bbox
from resilience_map.core.models import DatasetBundle

MAX_SELECTION_TRACTS = 500  # per-analysis / per-question ceiling (memory and model budget)


def select_bundle(bundle: DatasetBundle, bbox: tuple[float, ...]) -> DatasetBundle:
    west, south, east, north = validate_bbox(bbox)
    tracts = [t for t in bundle.tracts if west <= t.lon <= east and south <= t.lat <= north]
    if not tracts:
        raise ValueError("No downloaded tract centroids in selected area")
    if len(tracts) > MAX_SELECTION_TRACTS:
        raise ValueError(
            f"Selection has {len(tracts):,} tracts; the limit is {MAX_SELECTION_TRACTS}. "
            "Draw a smaller box on the map."
        )
    return bundle.model_copy(update={"tracts": tracts})
