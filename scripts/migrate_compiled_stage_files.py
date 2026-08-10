"""Alembic's counterpart for a project's WORKING COPY.

An alembic revision rewrites JSON payloads in the document store; the compiled
stage files under `<project>/compiled/` live on disk and no revision can reach
them. Both carry the same stage specs, so a stage-shape change strands the two
together and only one of them has a migration path. This script is that path.

Applied here, matching the revisions that do the same to the store:
  0004 — `primary_key` left the stage vocabulary, and TableSchema forbids extras,
         so a compiled file still carrying it no longer loads.
  0006 — the stored `output_schema` left too; a stage's output resolves from its
         `signature`, synthesized here from the outer the file stored.
  0008 — `name` became `description`: a stage has one name, its id.

Usage:  python -m scripts.migrate_compiled_stage_files [--apply] [--projects-dir PATH]
Without --apply it is a dry run and writes nothing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.core.paths import repo_root
from scripts.stage_description import (
    DescriptionUndeterminable,
    rename_name_to_description,
)
from scripts.stage_signatures import SignatureUndeterminable, add_signature

_KEY = "primary_key"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--projects-dir", type=Path, default=repo_root() / "examples")
    args = parser.parse_args()

    stale, refused = find_stale_stage_files(args.projects_dir)
    _report_refusals(refused, args.projects_dir)
    if not stale:
        print("every compiled stage file this can migrate is already in today's shape")
        return

    print(f"{len(stale)} file(s) {'-> rewriting' if args.apply else '(dry run)'}:")
    for path in stale:
        print(f"  {path.relative_to(args.projects_dir)}")
    if args.apply:
        for path in stale:
            _rewrite(path)
        print(f"rewrote {len(stale)} file(s)")


def find_stale_stage_files(projects_dir: Path) -> tuple[list[Path], list[tuple[Path, str]]]:
    """The files this can bring to today's shape, and the ones it refuses with why.

    Refuse the FILE, not the run: one stage a human must author must not hold back
    every other project's migration (the same rule the alembic revisions follow)."""
    stale: list[Path] = []
    refused: list[tuple[Path, str]] = []
    for path in sorted(projects_dir.glob("*/compiled/*.json")):
        try:
            if _migrate(_read(path)):
                stale.append(path)
        except (SignatureUndeterminable, DescriptionUndeterminable) as exc:
            refused.append((path, str(exc)))
    return stale, refused


def _report_refusals(refused: list[tuple[Path, str]], projects_dir: Path) -> None:
    if not refused:
        return
    print(f"{len(refused)} file(s) REFUSED and left untouched — a human must author "
          f"these before they will load:")
    for path, reason in refused:
        print(f"  {path.relative_to(projects_dir)}: {reason}")


def _rename_stage_types(node: Any) -> bool:
    """Only a `type` key is rewritten — a stage's own prose may name the old type."""
    found = False
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "type" and value in _RENAMED_TYPES:
                node[key] = _RENAMED_TYPES[value]
                found = True
            else:
                found |= _rename_stage_types(value)
    elif isinstance(node, list):
        for item in node:
            found |= _rename_stage_types(item)
    return found


_RENAMED_TYPES = {"python_frame_function": "pandas_frame_function"}


def _migrate(spec: Any) -> bool:
    """Bring one stage spec to today's shape; True if anything changed."""
    changed = _drop_primary_keys(spec) | _rename_stage_types(spec)
    if not isinstance(spec, dict):
        return changed
    return rename_name_to_description(spec) | add_signature(spec) | changed


def _rewrite(path: Path) -> None:
    spec = _read(path)
    _migrate(spec)
    # Matches loader.stage_to_json's format (indent=2, no trailing newline), so a
    # rewritten file differs from an app-written one only by the dropped key.
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _drop_primary_keys(node: Any) -> bool:
    """Remove every `primary_key` key anywhere in `node`; True if any was found."""
    found = False
    if isinstance(node, dict):
        found = node.pop(_KEY, _MISSING) is not _MISSING
        for value in node.values():
            found |= _drop_primary_keys(value)
    elif isinstance(node, list):
        for item in node:
            found |= _drop_primary_keys(item)
    return found


_MISSING = object()


if __name__ == "__main__":
    main()
