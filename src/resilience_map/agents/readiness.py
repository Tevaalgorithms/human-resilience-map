"""Deterministic evidence-readiness policy used by the LangGraph readiness role."""

from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from resilience_map.core.models import DatasetBundle, ScenarioConstraints
from resilience_map.gis.access import SPEED_KMH
from resilience_map.gis.routing import RoadRouter
from resilience_map.gis.selection import select_bundle
from resilience_map.optimization.eligibility import eligible_candidate_sites
from resilience_map.risk.service import score_dataset

ReadinessStatus = Literal["ready", "exploratory", "blocked"]


class FreshnessPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_max_age_days: int = Field(default=730, ge=1, le=3650)
    inventory_max_age_days: int = Field(default=365, ge=1, le=3650)
    road_max_age_days: int = Field(default=365, ge=1, le=3650)


class ReadinessReason(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str
    level: Literal["block", "warning", "information"]
    message: str
    affected_operation: str


class EvidenceReference(BaseModel):
    model_config = ConfigDict(frozen=True)
    reference: str
    name: str
    source: str | None = None
    retrieved_at: str | None = None
    observation_period: str | None = None
    geographic_coverage: str | None = None
    license: str | None = None


class ReadinessAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: ReadinessStatus
    operation: str
    dataset_version_id: str
    dataset_version: str
    scoring_version: str
    reasons: list[ReadinessReason]
    missing_inputs: list[str]
    warnings: list[str]
    evidence_references: list[EvidenceReference]
    suggested_next_actions: list[str]
    evaluated_at: str
    freshness_policy: FreshnessPolicy


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _references(bundle: DatasetBundle) -> list[EvidenceReference]:
    return [
        EvidenceReference(
            reference=f"source_manifest:{index}",
            name=str(source.get("name") or source.get("kind") or f"Source {index + 1}"),
            source=str(source["url"]) if source.get("url") else None,
            retrieved_at=str(source["retrieved_at"]) if source.get("retrieved_at") else None,
            observation_period=str(source["observation_period"])
            if source.get("observation_period")
            else None,
            geographic_coverage=str(source["geographic_coverage"])
            if source.get("geographic_coverage")
            else None,
            license=str(source["license"]) if source.get("license") else None,
        )
        for index, source in enumerate(bundle.source_manifest)
    ]


def _road_coverage_error(bundle: DatasetBundle, constraints: ScenarioConstraints) -> str | None:
    if constraints.access_model != "road_network":
        return None
    if bundle.roads is None:
        return "Network routing was requested but no pinned road snapshot is available"
    try:
        RoadRouter(bundle.roads, constraints.travel_mode)
        west, south, east, north = map(float, bundle.roads["coverage_bbox"])
        buffer_km = constraints.threshold_minutes / 60 * SPEED_KMH[constraints.travel_mode]
        for tract in bundle.tracts:
            dy = buffer_km / 110.5
            dx = dy / max(0.1, math.cos(math.radians(tract.lat)))
            if not (
                west <= tract.lon - dx
                and tract.lon + dx <= east
                and south <= tract.lat - dy
                and tract.lat + dy <= north
            ):
                return "Pinned road coverage lacks the required threshold-sized surrounding buffer"
    except (KeyError, TypeError, ValueError) as exc:
        return f"Pinned road snapshot is invalid for the requested mode: {exc}"
    return None


def assess_readiness(
    bundle: DatasetBundle,
    operation: str,
    constraints: ScenarioConstraints | None,
    policy: FreshnessPolicy,
    now: datetime | None = None,
) -> ReadinessAssessment:
    """Assess evidence without calculating or choosing sites."""
    now = now or datetime.now(UTC)
    if constraints is not None and constraints.bbox is not None:
        bundle = select_bundle(bundle, constraints.bbox)
    reasons: list[ReadinessReason] = []
    missing: list[str] = []
    warnings: list[str] = []
    actions: list[str] = []

    def add(code: str, level: str, message: str, affected: str, action: str | None = None) -> None:
        reasons.append(
            ReadinessReason(code=code, level=level, message=message, affected_operation=affected)
        )
        if level == "block":
            missing.append(message)
        elif level == "warning":
            warnings.append(message)
        if action and action not in actions:
            actions.append(action)

    if not bundle.tracts:
        add(
            "no_tracts",
            "block",
            "No tracts cover the selected area",
            operation,
            "Select an area with downloaded tract coverage",
        )

    score_results = score_dataset(bundle)
    missing_counts = Counter(key for _, result in score_results.values() for key in result.missing)
    if missing_counts:
        message = "Missing indicators: " + ", ".join(
            f"{key} ({count})" for key, count in sorted(missing_counts.items())
        )
        level = (
            "block"
            if operation in {"tract_explanation", "vulnerability_ranking"}
            and not any(result.score is not None for _, result in score_results.values())
            else "warning"
        )
        add(
            "missing_indicators",
            level,
            message,
            operation,
            "Acquire or review the missing historical indicators",
        )

    references = _references(bundle)
    for reference in references:
        retrieved = _timestamp(reference.retrieved_at)
        if not reference.retrieved_at:
            add(
                "source_date_missing",
                "warning",
                f"Retrieval date is missing for {reference.name}",
                operation,
                "Record a retrieval date in the source manifest",
            )
        elif retrieved is None:
            add(
                "invalid_source_date",
                "warning",
                f"Source date is invalid for {reference.name}",
                operation,
            )
        elif retrieved and retrieved > now + timedelta(days=1):
            add(
                "future_source_date",
                "block",
                f"Source date is in the future for {reference.name}",
                operation,
            )
        elif retrieved and now - retrieved > timedelta(days=policy.source_max_age_days):
            add(
                "stale_source",
                "warning",
                f"Source exceeds the {policy.source_max_age_days}-day freshness policy: {reference.name}",
                operation,
                "Review or refresh the source snapshot",
            )

    if operation == "site_selection" and constraints is not None:
        if bundle.inventory_status == "unknown":
            add(
                "inventory_unknown",
                "warning",
                "Cooling-center inventory is unknown; baseline access is exploratory",
                operation,
                "Import a locally verified inventory",
            )
        elif bundle.inventory_status == "partial":
            add(
                "inventory_partial",
                "warning",
                "Cooling-center inventory is explicitly partial",
                operation,
                "Complete and locally verify the inventory",
            )
        elif bundle.inventory_status == "confirmed_absent":
            add(
                "inventory_confirmed_absent",
                "information",
                "A documented inventory reports no cooling centers",
                operation,
            )
        elif bundle.inventory_status == "verified" and not bundle.centers:
            add(
                "verified_inventory_empty",
                "block",
                "Verified inventory is empty without confirmed-absent status",
                operation,
                "Correct the inventory status or records",
            )
        unknown_centers = [c.center_id for c in bundle.centers if c.opening_status == "unknown"]
        if unknown_centers:
            add(
                "opening_status_unknown",
                "warning",
                f"Opening status is unknown for {len(unknown_centers)} listed center(s)",
                operation,
                "Confirm center operating status and date",
            )
        unconfirmed_centers = [c.center_id for c in bundle.centers if c.last_confirmed is None]
        if unconfirmed_centers:
            add(
                "inventory_confirmation_missing",
                "warning",
                f"Confirmation date is missing for {len(unconfirmed_centers)} center(s)",
                operation,
                "Record local confirmation dates without inferring operating status",
            )
        old_confirmations = [
            c.center_id
            for c in bundle.centers
            if c.last_confirmed
            and now.date() - c.last_confirmed > timedelta(days=policy.inventory_max_age_days)
        ]
        if old_confirmations:
            add(
                "inventory_confirmation_stale",
                "warning",
                f"Confirmation exceeds the {policy.inventory_max_age_days}-day policy for {len(old_confirmations)} center(s)",
                operation,
                "Reconfirm center records",
            )
        try:
            candidates = eligible_candidate_sites(bundle.candidates, constraints.candidate_site_ids)
        except ValueError as exc:
            add(
                "invalid_candidate_subset",
                "block",
                str(exc),
                operation,
                "Choose candidate IDs from the pinned dataset",
            )
            candidates = []
        if not candidates:
            add(
                "no_eligible_candidates",
                "block",
                "No accessible candidates remain under the requested subset",
                operation,
                "Supply locally reviewed accessible candidates with capacity and cost",
            )
        elif any(
            c.capacity <= 0 or not math.isfinite(c.cost_usd) or c.cost_usd < 0 for c in candidates
        ):
            add(
                "invalid_candidate_fields",
                "block",
                "Candidate capacity or cost fields are invalid",
                operation,
                "Correct candidate capacity and cost evidence",
            )
        road_error = _road_coverage_error(bundle, constraints)
        if road_error:
            add(
                "road_routing_unavailable",
                "block",
                road_error,
                operation,
                "Prepare a pinned, buffered road snapshot for the requested mode",
            )
        elif constraints.access_model == "road_network" and bundle.roads is not None:
            road_retrieved = _timestamp(bundle.roads.get("retrieved_at"))
            if road_retrieved is None:
                add(
                    "road_date_missing",
                    "warning",
                    "Pinned road snapshot has no valid retrieval date",
                    operation,
                    "Record road retrieval and OSM snapshot dates",
                )
            elif road_retrieved > now + timedelta(days=1):
                add(
                    "road_date_future",
                    "block",
                    "Pinned road snapshot retrieval date is in the future",
                    operation,
                    "Correct the road snapshot provenance",
                )
            elif now - road_retrieved > timedelta(days=policy.road_max_age_days):
                add(
                    "road_snapshot_stale",
                    "warning",
                    f"Road snapshot exceeds the {policy.road_max_age_days}-day policy",
                    operation,
                    "Review or refresh the buffered road snapshot off-host",
                )

    status: ReadinessStatus = (
        "blocked"
        if any(reason.level == "block" for reason in reasons)
        else ("exploratory" if any(reason.level == "warning" for reason in reasons) else "ready")
    )
    return ReadinessAssessment(
        status=status,
        operation=operation,
        dataset_version_id=bundle.dataset_version_id,
        dataset_version=bundle.version,
        scoring_version=bundle.scoring_version,
        reasons=reasons,
        missing_inputs=missing,
        warnings=warnings,
        evidence_references=references,
        suggested_next_actions=actions,
        evaluated_at=now.isoformat(),
        freshness_policy=policy,
    )
