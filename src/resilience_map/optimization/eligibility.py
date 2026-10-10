"""Shared eligibility policy, independent of routing and optimization calculations."""

from __future__ import annotations

from collections.abc import Sequence

from resilience_map.core.models import CandidateSite, CoolingCenter


def usable_baseline_centers(centers: Sequence[CoolingCenter]) -> list[CoolingCenter]:
    """Exclude confirmed closures; unknown status remains explicitly uncertain."""
    return [center for center in centers if center.opening_status != "confirmed_closed"]


def eligible_candidate_sites(
    candidates: Sequence[CandidateSite], requested_ids: Sequence[str] | None
) -> list[CandidateSite]:
    """Validate against the full inventory, then apply accessibility and requested IDs.

    An absent filter includes accessible candidates. An empty filter includes none.
    Known but inaccessible requested sites remain ineligible rather than unknown.
    """
    requested = set(requested_ids) if requested_ids is not None else None
    if requested is not None:
        unknown = requested - {site.site_id for site in candidates}
        if unknown:
            raise ValueError(f"Unknown candidate sites: {sorted(unknown)}")
    return [
        site
        for site in candidates
        if site.accessible and (requested is None or site.site_id in requested)
    ]


def vary_candidate_capacities(
    candidates: Sequence[CandidateSite], multiplier: float
) -> list[CandidateSite]:
    """Apply a disclosed deterministic capacity factor; never creates candidate facilities."""
    if not 0.5 <= multiplier <= 1.5:
        raise ValueError("Candidate capacity multiplier must be from 0.5 to 1.5")
    return [
        site.model_copy(update={"capacity": max(1, round(site.capacity * multiplier))})
        for site in candidates
    ]
