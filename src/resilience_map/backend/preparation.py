"""Single bounded asynchronous county preparation process; no automatic publication.

Status survives restarts on disk. A timed-out/restarted job is never marked ready.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from resilience_map.core.geography import BoundingBox, validate_bbox
from resilience_map.ingestion.sources import REQUIRED_FILES

ROOT = Path(os.getenv("HRM_PREPARATION_PATH", ".data/preparation"))
MAX_JOBS = 16
MAX_STORAGE_BYTES = 200_000_000
MAX_FILE_BYTES = 20_000_000
PROCESS_TIMEOUT_SECONDS = 180
INTERRUPTED_AFTER_SECONDS = 200
_lock = threading.Lock()


def prepare(
    state: str,
    county: str,
    owner: str,
    bbox: BoundingBox | None = None,
    include_cooling: bool = False,
) -> str:
    if include_cooling and (state, county) != ("06", "037"):
        raise HTTPException(
            422,
            "Online cooling inventory is currently supported for Los Angeles County (06/037) only; upload a local inventory for other regions",
        )
    if bbox is not None:
        bbox = validate_bbox(bbox)
    if os.getenv("HRM_ENABLE_PREPARATION", "false").lower() != "true":
        raise HTTPException(
            409, "Preparation disabled; enable on a dedicated bounded preparation service"
        )
    ROOT.mkdir(parents=True, exist_ok=True)
    if (
        len(list(ROOT.glob("*/status.json"))) >= MAX_JOBS
        or sum(p.stat().st_size for p in ROOT.rglob("*") if p.is_file()) > MAX_STORAGE_BYTES
    ):
        raise HTTPException(
            429, "Preparation storage limit reached; operator must archive old jobs"
        )
    if not _lock.acquire(blocking=False):
        raise HTTPException(429, "One preparation is already running")
    ident = uuid.uuid4().hex
    directory = ROOT / ident
    try:
        directory.mkdir()
    except OSError:
        _lock.release()
        raise
    status: dict[str, Any] = {
        "id": ident,
        "created_at": datetime.now(UTC).isoformat(),
        "owner": owner,
        "state": "queued",
        "source": "Census TIGERweb + ACS",
        "coverage": f"{state}{county}",
        "selection_bbox": bbox,
        "boundary_vintage": "2023" if bbox is not None else "current TIGERweb",
        "acs_period": "2019-2023",
        "analysis_ready": False,
        "cooling_requested": include_cooling,
        "missing_inputs": [
            "historical heat",
            "canopy",
            "verified cooling inventory",
            "candidate costs/capacities",
            "buffered roads",
        ],
    }

    def save() -> None:
        tmp = directory / "status.tmp"
        tmp.write_text(json.dumps(status))
        tmp.replace(directory / "status.json")

    try:
        save()
    except OSError:
        _lock.release()
        raise

    def run() -> None:
        try:
            status["state"] = "running"
            save()
            proc = subprocess.Popen(  # noqa: S603 - fixed command with validated FIPS
                [
                    sys.executable,
                    "-m",
                    "resilience_map.backend.preparation",
                    state,
                    county,
                    str(directory / "sources"),
                    *(
                        [",".join(map(str, bbox))]
                        if bbox is not None
                        else ([""] if include_cooling else [])
                    ),
                    *(["cooling"] if include_cooling else []),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                code = proc.wait(timeout=PROCESS_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                code = -1
            outputs = directory / "sources"
            complete = all(
                (outputs / name).is_file() and (outputs / name).stat().st_size <= MAX_FILE_BYTES
                for name in REQUIRED_FILES
            )
            status["state"] = "downloaded_unpublished" if code == 0 and complete else "failed"
            save()
        except OSError:
            status["state"] = "failed"
            save()
        finally:
            _lock.release()

    try:
        threading.Thread(target=run, daemon=True).start()
    except RuntimeError:
        _lock.release()
        status["state"] = "failed"
        save()
        raise
    return ident


def get_status(ident: str, owner: str) -> dict[str, Any]:
    if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
        raise HTTPException(404, "Preparation not found")
    path = ROOT / ident / "status.json"
    if not path.is_file():
        raise HTTPException(404, "Preparation not found")
    status = json.loads(path.read_text())
    if status["owner"] != owner:
        raise HTTPException(404, "Preparation not found")
    if (
        status["state"] in {"queued", "running"}
        and (datetime.now(UTC) - datetime.fromisoformat(status["created_at"])).total_seconds()
        > INTERRUPTED_AFTER_SECONDS
    ):
        status["state"] = "interrupted"
    return {k: v for k, v in status.items() if k != "owner"}


def download_sources(ident: str, owner: str) -> bytes:
    """Owner-scoped bounded source package, with no guessed local inventories."""
    status = get_status(ident, owner)
    if status["state"] != "downloaded_unpublished":
        raise HTTPException(409, "Source download is not complete")
    directory = ROOT / ident / "sources"
    paths = [directory / name for name in REQUIRED_FILES]
    if any(not p.is_file() or p.is_symlink() or p.stat().st_size > MAX_FILE_BYTES for p in paths):
        raise HTTPException(409, "Source package is missing or exceeds limits")
    if sum(p.stat().st_size for p in paths) > 80_000_000:
        raise HTTPException(413, "Source package exceeds 80 MB")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, arcname=path.name)
    return output.getvalue()


if __name__ == "__main__":
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (20_000_000, 20_000_000))
    from resilience_map.core.geography import parse_bbox
    from resilience_map.ingestion.census import prepare_directory

    prepare_directory(
        sys.argv[3],
        sys.argv[1],
        [sys.argv[2]],
        2023,
        simplify_degrees=0.00005,
        bbox=parse_bbox(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None,
    )
    if len(sys.argv) > 5 and sys.argv[5] == "cooling":
        from resilience_map.ingestion.california import add_la_cooling_inventory

        add_la_cooling_inventory(sys.argv[3])
