"""Source adapters. An adapter only *acquires* bytes; parsing happens from the stored snapshot,
so the same snapshot always yields the same dataset."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

REQUIRED_FILES = (
    "tracts.geojson",
    "indicators.csv",
    "centers.csv",
    "candidates.csv",
    "manifest.json",
)


@dataclass(frozen=True)
class Snapshot:
    study_area_id: str
    study_area_name: str
    is_synthetic: bool
    files: dict[str, bytes]
    acquired_at: str


class SourceAdapter(Protocol):
    def fetch(self) -> Snapshot: ...


class FileAdapter:
    """Reads a directory of already-acquired source files (see docs/data_inventory.md)."""

    def __init__(self, directory: str, study_area_id: str, study_area_name: str) -> None:
        self._dir, self._id, self._name = Path(directory), study_area_id, study_area_name

    def fetch(self) -> Snapshot:
        missing = [f for f in REQUIRED_FILES if not (self._dir / f).is_file()]
        if missing:
            raise FileNotFoundError(f"Missing source files in {self._dir}: {missing}")
        return Snapshot(
            self._id,
            self._name,
            False,
            {
                f: (self._dir / f).read_bytes()
                for f in (*REQUIRED_FILES, "roads.json")
                if (self._dir / f).is_file()
            },
            datetime.now(UTC).isoformat(timespec="seconds"),
        )
