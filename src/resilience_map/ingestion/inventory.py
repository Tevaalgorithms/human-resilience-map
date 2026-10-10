"""Local inventory validation; unknown values stay unknown."""

from datetime import UTC, datetime

from resilience_map.core.models import DatasetBundle


def validate_inventory(bundle: DatasetBundle) -> None:
    if bundle.inventory_status == "confirmed_absent" and bundle.centers:
        raise ValueError("Confirmed absence conflicts with inventory records")
    evidence = [s for s in bundle.source_manifest if s.get("kind") == "cooling_inventory"]
    if bundle.inventory_status in {"verified", "confirmed_absent"} and not any(
        s.get("verified_by")
        and s.get("observation_period")
        and s.get("geographic_coverage")
        and s.get("url")
        for s in evidence
    ):
        raise ValueError("Verified inventory requires reviewer, date, coverage and source")
    ids = [c.center_id for c in bundle.centers]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate cooling center ids")
    for c in bundle.centers:
        if c.last_confirmed and c.last_confirmed > datetime.now(UTC).date():
            raise ValueError("Inventory confirmation date is in the future")
        if (bundle.inventory_status == "verified" or c.opening_status != "unknown") and not (
            c.source and c.last_confirmed and c.provenance
        ):
            raise ValueError("Verified centers/status require source, confirmation and provenance")
