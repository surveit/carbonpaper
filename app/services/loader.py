"""Canonical load + save for a project's compiled stage files.

One JSON file per stage under `<project>/compiled/`. This module is the ONE
place that knows the on-disk stage format — both directions. Everything past it
speaks `Stage` objects; nothing else should call `model_dump_json` on a stage or
glob `compiled/*.json`, so a format change (or the planned rename) touches only
this file.

Read:
  - load_compiled_dir: tolerant, per-file — for the viewer, which renders
    problems rather than crashing.
  - load_workflow: strict — for the runner, which refuses to execute
    a workflow with any invalid stage or cross-stage issue.

Serialize / save:
  - stage_to_spec_dict / stage_to_json: the canonical data + text forms.
  - find_stage_file / write_stage: locate and overwrite one stage's file.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.models.workflow import validate_workflow
from app.models.schema import format_errors
from app.models.stage import Stage, parse_stage


@dataclass
class CompiledStageFile:
    """One compiled file: its parsed Stage (None if invalid) and any issues."""
    filename: str
    stage: Stage | None = None
    issues: list[str] = field(default_factory=list)


class WorkflowLoadError(Exception):
    """The compiled workflow failed validation; `issues` lists every problem found."""

    def __init__(self, compiled_dir: Path, issues: list[str]):
        self.issues = issues
        super().__init__(
            f"{compiled_dir}: {len(issues)} validation issue(s):\n  "
            + "\n  ".join(issues)
        )


def load_compiled_dir(compiled_dir: Path) -> list[CompiledStageFile]:
    entries: list[CompiledStageFile] = []
    for f in sorted(compiled_dir.glob("*.json")):
        entry = CompiledStageFile(filename=f.name)
        entries.append(entry)
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            entry.issues.append(f"JSON parse error: {exc}")
            continue
        if not data:
            entry.issues.append("file contains no stage object")
            continue
        try:
            entry.stage = parse_stage(data)
        except ValidationError as err:
            entry.issues.extend(format_errors(err))
    return entries


def load_workflow(project_dir: Path) -> list[Stage]:
    compiled_dir = project_dir / "compiled"
    entries = load_compiled_dir(compiled_dir)
    issues = [f"{e.filename}: {i}" for e in entries for i in e.issues]
    if not entries:
        issues.append(f"no compiled stage files found in {compiled_dir}")
    stages = [e.stage for e in entries if e.stage is not None]
    issues += validate_workflow(stages)
    if issues:
        raise WorkflowLoadError(compiled_dir, issues)
    return stages


# ─── Serialize & save ────────────────────────────────────────────────────────

def stage_to_spec_dict(stage: Stage) -> dict[str, Any]:
    """The canonical dict form of a stage: field aliases restored (`schema`, not
    `table_schema`), unset optionals dropped, enums/nested models JSON-normalised.
    This is the ONE definition of 'a stage as data' — the on-disk JSON is a dump
    of it, the belief hash is computed over it, and the raw-spec views render it,
    so all three move together if the shape changes."""
    return stage.model_dump(mode="json", by_alias=True, exclude_none=True)


def stage_to_json(stage: Stage) -> str:
    """The canonical on-disk JSON text for one compiled stage — an indented dump
    equal to `json.dumps(stage_to_spec_dict(stage))`. The single source of the
    persisted format; write_stage and the raw-spec endpoints go through it."""
    return stage.model_dump_json(indent=2, by_alias=True, exclude_none=True)


def find_stage_file(compiled_dir: Path, stage_id: str) -> Path | None:
    """The compiled file whose stage carries this id, or None. Reads each file
    only far enough to match the id (one stage per file, by convention)."""
    for f in sorted(compiled_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("id") == stage_id:
            return f
    return None


def write_stage(path: Path, stage: Stage) -> None:
    """Persist one validated stage to `path` in the canonical on-disk JSON."""
    path.write_text(stage_to_json(stage), encoding="utf-8")
