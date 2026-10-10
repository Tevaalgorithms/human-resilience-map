"""Remove only allowlisted development artifacts; never runtime data or environments."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT_ARTIFACTS = (".coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache", "htmlcov")
CODE_DIRECTORIES = ("src", "tests", "scripts", "deploy")


def generated_artifacts(root: Path, *, include_terraform: bool = False) -> list[Path]:
    """Enumerate artifacts without following directory symlinks."""
    artifacts = [root / name for name in ROOT_ARTIFACTS if (root / name).exists()]
    for name in CODE_DIRECTORIES:
        directory = root / name
        if directory.is_dir() and not directory.is_symlink():
            artifacts.extend(directory.rglob("__pycache__"))
    infrastructure = root / "infrastructure"
    if include_terraform and infrastructure.is_dir() and not infrastructure.is_symlink():
        artifacts.extend(infrastructure.rglob(".terraform"))
    return sorted(path for path in artifacts if not path.is_symlink())


def clean(root: Path, *, dry_run: bool = False, include_terraform: bool = False) -> list[Path]:
    artifacts = generated_artifacts(root, include_terraform=include_terraform)
    for path in artifacts:
        if not dry_run:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    return artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="list artifacts without deleting")
    parser.add_argument(
        "--include-terraform", action="store_true", help="also remove downloaded .terraform caches"
    )
    args = parser.parse_args()
    for path in clean(PROJECT_ROOT, dry_run=args.dry_run, include_terraform=args.include_terraform):
        print(path.relative_to(PROJECT_ROOT))


if __name__ == "__main__":
    main()
