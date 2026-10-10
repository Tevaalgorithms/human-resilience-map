"""Bounded US planning selection; destinations and roads remain buffered/full snapshot."""

from resilience_map.core.geography import validate_bbox as validate_bbox
from resilience_map.core.models import DatasetBundle


def select_bundle(bundle: DatasetBundle, bbox: tuple[float, ...]) -> DatasetBundle:
    west, south, east, north = validate_bbox(bbox)
    tracts = [t for t in bundle.tracts if west <= t.lon <= east and south <= t.lat <= north]
    if not tracts:
        raise ValueError("No downloaded tract centroids in selected area")
    if len(tracts) > 500:
        raise ValueError("Selection exceeds 500 tract demonstration limit")
    return bundle.model_copy(update={"tracts": tracts})
