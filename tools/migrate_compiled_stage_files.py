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

Usage:  python -m tools.migrate_compiled_stage_files [--apply] [--projects-dir PATH]
Without --apply it is a dry run and writes nothing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.core.paths import repo_root
from tools.stage_signatures import add_signature

_KEY = "primary_key"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--projects-dir", type=Path, default=repo_root() / "examples")
    args = parser.parse_args()

    stale = find_stale_stage_files(args.projects_dir)
    if not stale:
        print("every compiled stage file is already in today's shape")
        return

    print(f"{len(stale)} file(s) {'-> rewriting' if args.apply else '(dry run)'}:")
    for path in stale:
        print(f"  {path.relative_to(args.projects_dir)}")
    if args.apply:
        for path in stale:
            _rewrite(path)
        print(f"rewrote {len(stale)} file(s)")


def find_stale_stage_files(projects_dir: Path) -> list[Path]:
    """Every compiled stage file under `projects_dir` today's model cannot load."""
    return [path for path in sorted(projects_dir.glob("*/compiled/*.json"))
            if _migrate(_read(path))]


def _migrate(spec: Any) -> bool:
    """Bring one stage spec to today's shape; True if anything changed."""
    changed = _drop_primary_keys(spec)
    return add_signature(spec) | changed if isinstance(spec, dict) else changed


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
