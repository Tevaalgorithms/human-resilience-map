"""Bounded, owner-scoped upload previews, published through immutable ingestion."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from resilience_map.core.object_store import ObjectStore
from resilience_map.ingestion.pipeline import parse_snapshot, run_ingestion
from resilience_map.ingestion.sources import REQUIRED_FILES, Snapshot
from resilience_map.verification.checks import verify_dataset

ROOT = Path(os.getenv("HRM_IMPORT_PATH", ".data/imports"))
MAX_UPLOAD_BYTES = 20_000_000
MAX_PACKAGE_BYTES = 80_000_000
ALLOWED_FILES = (*REQUIRED_FILES, "roads.json")
_lock = threading.Lock()


def source_files(dataset: dict[str, Any], store: ObjectStore) -> dict[str, bytes]:
    """Load original immutable bytes; never rebuild geometries from map simplification."""
    source = next((s for s in dataset["source_manifest"] if s.get("snapshot_sha256")), None)
    if not source or not re.fullmatch(r"[a-f0-9]{64}", str(source["snapshot_sha256"])):
        raise HTTPException(409, "Original snapshot unavailable; upload a complete region ZIP")
    files = {}
    for name in ALLOWED_FILES:
        if name not in source.get("checksums", {}):
            continue
        key = f"raw/{dataset['study_area_id']}/{source['snapshot_sha256']}/{name}"
        if not store.exists(key):
            raise HTTPException(409, "Original snapshot unavailable; upload a complete region ZIP")
        files[name] = store.get(key)
    check_files(files)
    return files


def check_files(files: dict[str, bytes]) -> None:
    if any(name not in files for name in REQUIRED_FILES):
        raise ValueError(f"Package requires: {', '.join(REQUIRED_FILES)}")
    if any(len(value) > MAX_UPLOAD_BYTES for value in files.values()):
        raise ValueError("Each source file must be at most 20 MB")
    if sum(map(len, files.values())) > MAX_PACKAGE_BYTES:
        raise ValueError("Expanded source package exceeds 80 MB")


def unpack(data: bytes) -> dict[str, bytes]:
    """Read known root entries only, without extracting user paths or ZIP links."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or any(name not in ALLOWED_FILES for name in names):
                raise ValueError(
                    "ZIP must contain unique source files at its root, with no extra entries"
                )
            if any(e.file_size > MAX_UPLOAD_BYTES for e in entries):
                raise ValueError("Each expanded file must be at most 20 MB")
            if sum(e.file_size for e in entries) > MAX_PACKAGE_BYTES:
                raise ValueError("Expanded source package exceeds 80 MB")
            files = {e.filename: archive.read(e) for e in entries}
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError("Invalid, encrypted or unsupported ZIP") from exc
    check_files(files)
    return files


def replace_cooling(files: dict[str, bytes], data: bytes, source: str) -> dict[str, bytes]:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not {"center_id", "name", "lon", "lat"}.issubset(reader.fieldnames or []):
        raise ValueError("Cooling CSV requires center_id,name,lon,lat; capacity may be blank")
    rows = list(reader)
    if len(rows) > 5000:
        raise ValueError("Cooling upload exceeds 5,000 records")
    if any(
        not (r.get("center_id") or "").strip() or not (r.get("name") or "").strip() for r in rows
    ):
        raise ValueError("Every cooling center requires an ID and name")
    manifest = json.loads(files["manifest.json"])
    manifest = [entry for entry in manifest if entry.get("kind") != "cooling_inventory"]
    manifest.append(
        {
            "kind": "cooling_inventory",
            "name": "Uploaded local cooling inventory",
            "inventory_status": "partial" if rows else "unknown",
            "url": source,
            "license": "Not supplied; confirm with inventory owner",
            "geographic_coverage": "Selected study area; completeness not established",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "verified_by": None,
        }
    )
    return {**files, "centers.csv": text.encode(), "manifest.json": json.dumps(manifest).encode()}


def stage(files: dict[str, bytes], area: str, name: str, owner: str) -> dict[str, Any]:
    check_files(files)
    bundle, geometries = parse_snapshot(files)
    report = verify_dataset(bundle, geometries).to_dict()
    status: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "owner": owner,
        "study_area_id": area,
        "study_area_name": name,
        "created_at": datetime.now(UTC).isoformat(),
        "state": "validated" if report["passed"] else "needs_data",
        "report": report,
        "tracts": len(bundle.tracts),
        "centers": len(bundle.centers),
        "inventory_status": bundle.inventory_status,
        "notice": "Publication creates a new dataset version. Cooling CSV replaces the entire study-area inventory; upload alone does not establish local verification.",
    }
    with _lock:
        ROOT.mkdir(parents=True, exist_ok=True)
        used = sum(p.stat().st_size for p in ROOT.rglob("*") if p.is_file())
        if (
            len(list(ROOT.glob("*/status.json"))) >= 16
            or used + sum(map(len, files.values())) > 200_000_000
        ):
            raise HTTPException(
                429, "Import storage limit reached; operator must archive old uploads"
            )
        directory = ROOT / status["id"]
        directory.mkdir()
        for filename, data in files.items():
            (directory / filename).write_bytes(data)
        (directory / "status.json").write_text(json.dumps(status))
    return {k: v for k, v in status.items() if k != "owner"}


def get_status(ident: str, owner: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", ident):
        raise HTTPException(404, "Import not found")
    path = ROOT / ident / "status.json"
    if not path.is_file():
        raise HTTPException(404, "Import not found")
    status = json.loads(path.read_text())
    if status["owner"] != owner:
        raise HTTPException(404, "Import not found")
    return {k: v for k, v in status.items() if k != "owner"}


@dataclass
class UploadAdapter:
    snapshot: Snapshot

    def fetch(self) -> Snapshot:
        return self.snapshot


def publish(ident: str, owner: str, store: ObjectStore) -> dict[str, Any]:
    with _lock:
        status = get_status(ident, owner)
        if not status["report"]["passed"]:
            raise HTTPException(409, "Complete missing inputs and upload again before publication")
        directory = ROOT / ident
        files = {
            name: (directory / name).read_bytes()
            for name in ALLOWED_FILES
            if (directory / name).is_file()
        }
        result = run_ingestion(
            UploadAdapter(
                Snapshot(
                    status["study_area_id"],
                    status["study_area_name"],
                    False,
                    files,
                    status["created_at"],
                )
            ),
            store,
        )
        if result.status == "failed":
            raise HTTPException(422, result.report)
        return {
            "status": result.status,
            "dataset_version_id": result.dataset_version_id,
            "version": result.version,
            "study_area_id": status["study_area_id"],
            "report": result.report,
        }
