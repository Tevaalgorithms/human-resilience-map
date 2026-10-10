"""Dataset-level scoring: nearest-center access + resilience index for every tract."""

from __future__ import annotations

from resilience_map.core.models import DatasetBundle
from resilience_map.gis.access import nearest_km
from resilience_map.risk.scoring import SCORING_V1, ScoreResult, ScoringModel, score_tract


def score_dataset(
    bundle: DatasetBundle, model: ScoringModel = SCORING_V1
) -> dict[str, tuple[float | None, ScoreResult]]:
    """Return ``tract_id -> (nearest_center_km, ScoreResult)``."""
    centers = [(c.lon, c.lat) for c in bundle.centers]
    out: dict[str, tuple[float | None, ScoreResult]] = {}
    for t in bundle.tracts:
        km = nearest_km(t.lon, t.lat, centers)
        out[t.tract_id] = (km, score_tract(t.indicators, km, model))
    return out


def vulnerability_map(scores: dict[str, tuple[float | None, ScoreResult]]) -> dict[str, float]:
    """Vulnerability in 0..1 = 1 - score/100. Tracts without a score are omitted."""
    return {tid: 1.0 - r.score / 100.0 for tid, (_, r) in scores.items() if r.score is not None}
