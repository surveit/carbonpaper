"""Alembic's counterpart for a project's WORKING COPY.

An alembic revision rewrites JSON payloads in the document store; the compiled
stage files under `<project>/compiled/` live on disk and no revision can reach
them. Both carry the same stage specs, so a stage-shape change strands the two
together and only one of them has a migration path. This script is that path,
and `rewrite_stale_stage_files` is the seam a revision calls to walk the same
files with its own rewrite instead of the whole catalogue below.

Applied here, matching the revisions that do the same to the store:
  0004 — `primary_key` left the stage vocabulary, and TableSchema forbids extras,
         so a compiled file still carrying it no longer loads.
  0006 — the stored `output_schema` left too; a stage's output resolves from its
         `signature`, synthesized here from the outer the file stored.
  0008 — `name` became `description`: a stage has one name, its id.
  0010 — a filter_rows/human_review_queue signature reading nothing no longer
         loads; its reads become the whole anchor edge.
  0011 — an input's stored `schema` left; the graph resolves it.
  0012 — a publish stage's `template` left; what it said is kept as a
         compiler_note, since the markup it was named for now lives in
         `function.code`.
  0013 — `llm.model` became required; a stage that named none is stamped with
         the model it has been running on (app.runtime.options.DEFAULT_MODEL).

Usage:  python -m scripts.migrate_compiled_stage_files [--apply] [--projects-dir PATH]
Without --apply it is a dry run and writes nothing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from app.runtime.options import DEFAULT_MODEL
from app.services.workspace import configure_projects_dir_from_env, projects_dir
from scripts.llm_model import LlmConfigUnreadable, stamp_llm_model
from scripts.stage_description import (
    DescriptionUndeterminable,
    rename_name_to_description,
)
from scripts.publish_template import (
    PublishTemplateUnreadable,
    move_publish_template_to_notes,
)
from scripts.stage_input_schemas import (
    InputRefUnreadable,
    drop_stored_input_schemas,
)
from scripts.stage_signatures import (
    SignatureUndeterminable,
    add_signature,
    backfill_anchor_reads,
)

_KEY = "primary_key"

# One stage spec in, True if it changed it. A revision hands its own rewrite to
# `rewrite_stale_stage_files`; this module hands the whole catalogue, `_migrate`.
StageSpecRewrite = Callable[[Any], bool]

# Refused rather than guessed: the file is left as it is and a human authors it.
REFUSALS = (SignatureUndeterminable, DescriptionUndeterminable,
            InputRefUnreadable, PublishTemplateUnreadable, LlmConfigUnreadable)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--projects-dir", type=Path, default=None,
                        help="default: the projects root, CARBON_PAPER_PROJECTS_DIR "
                             "or ~/.carbonpaper/examples")
    args = parser.parse_args()
    configure_projects_dir_from_env()
    root = args.projects_dir or projects_dir()

    stale = rewrite_stale_stage_files(root, _migrate, apply=args.apply)
    if not stale:
        print("every compiled stage file this can migrate is already in today's shape")


def rewrite_stale_stage_files(
    projects_dir: Path, rewrite: StageSpecRewrite, *, apply: bool
) -> list[Path]:
    """Apply `rewrite` to every compiled stage file under `projects_dir`; the files it changed.

    The survey runs to completion before the first write, so an unreadable file
    leaves every project's compiled/ untouched."""
    stale, refused = find_stale_stage_files(projects_dir, rewrite)
    _report_refusals(refused, projects_dir)
    if not stale:
        return stale
    print(f"{len(stale)} compiled stage file(s) {'-> rewriting' if apply else '(dry run)'}:")
    for path in stale:
        print(f"  {path.relative_to(projects_dir)}")
    if apply:
        for path in stale:
            _rewrite(path, rewrite)
        print(f"rewrote {len(stale)} file(s)")
    return stale


def find_stale_stage_files(
    projects_dir: Path, rewrite: StageSpecRewrite
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """The files `rewrite` would change, and the ones it refuses with why.

    Refuse the FILE, not the run: one stage a human must author must not hold back
    every other project's migration (the same rule the alembic revisions follow)."""
    stale: list[Path] = []
    refused: list[tuple[Path, str]] = []
    for path in sorted(projects_dir.glob("*/compiled/*.json")):
        try:
            if rewrite(_read(path)):
                stale.append(path)
        except REFUSALS as exc:
            refused.append((path, str(exc)))
    return stale, refused


def _report_refusals(refused: list[tuple[Path, str]], projects_dir: Path) -> None:
    if not refused:
        return
    print(f"{len(refused)} file(s) REFUSED and left untouched — a human must author "
          f"these before they will load:")
    for path, reason in refused:
        print(f"  {path.relative_to(projects_dir)}: {reason}")


def _migrate(spec: Any) -> bool:
    """Bring one stage spec to today's shape; True if anything changed."""
    changed = _drop_primary_keys(spec)
    if not isinstance(spec, dict):
        return changed
    # `|` evaluates left to right, and the stored input schemas are what 0006 and
    # 0010 read to synthesize a signature from, so dropping them comes last.
    return (
        rename_name_to_description(spec)
        | add_signature(spec)
        | backfill_anchor_reads(spec)
        | drop_stored_input_schemas(spec)
        | move_publish_template_to_notes(spec)
        | stamp_llm_model(spec, DEFAULT_MODEL)
        | changed
    )


def _rewrite(path: Path, rewrite: StageSpecRewrite) -> None:
    spec = _read(path)
    rewrite(spec)
    # Matches loader.stage_to_json's format (indent=2, no trailing newline), so a
    # rewritten file differs from an app-written one only by the dropped key.
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")


def _read(path: Path) -> Any:
    # Raised out of the survey, which runs to completion before the first write, so
    # one unreadable file leaves every other project's compiled/ untouched.
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not JSON: {exc}") from exc


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
