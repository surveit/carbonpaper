"""Canonical load + save for a project's compiled stage files.

One JSON file per stage under `<project>/compiled/`. This module is the ONE place
that knows the on-disk stage format, in both directions: nothing else should call
`model_dump_json` on a stage or glob `compiled/*.json`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.models.workflow import Workflow, validate_workflow
from app.models.stage import Stage
from app.core.utils import format_errors

from .errors import WorkflowLoadError


@dataclass
class CompiledStageFile:
    """One compiled file: its parsed Stage (None if invalid) and any issues."""
    filename: str
    stage: Stage | None = None
    issues: list[str] = field(default_factory=list)


def list_stage_files(compiled_dir: Path) -> list[Path]:
    """Every compiled stage file in `compiled_dir`, in filename order — the ONE
    definition of which files are stages, so callers never glob the dir themselves.
    An absent dir lists as empty: a project has no stage files until its first stage
    is written. Says nothing about whether the files parse or validate."""
    return sorted(compiled_dir.glob("*.json"))


def load_compiled_dir(compiled_dir: Path) -> list[CompiledStageFile]:
    entries: list[CompiledStageFile] = []
    for f in list_stage_files(compiled_dir):
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
            entry.stage = Stage.model_validate(data)
        except ValidationError as err:
            entry.issues.extend(format_errors(err))
    return entries


def load_workflow_object(project_dir: Path) -> Workflow:
    """Strict load of a project's compiled workflow as one in-memory `Workflow`
    object: parse every stage, collect ALL issues (per-file schema errors and
    cross-stage graph problems), and raise WorkflowLoadError if the dir is empty or
    anything is invalid — so the runner and version snapshotter refuse to execute or
    freeze an unloadable workflow. The single strict entry point; `load_workflow`
    (the list accessor) delegates here."""
    compiled_dir = project_dir / "compiled"
    entries = load_compiled_dir(compiled_dir)
    issues = [f"{e.filename}: {i}" for e in entries for i in e.issues]
    if not entries:
        issues.append(f"no compiled stage files found in {compiled_dir}")
    stages = [e.stage for e in entries if e.stage is not None]
    issues += validate_workflow(stages)
    if issues:
        raise WorkflowLoadError(compiled_dir, issues)
    return Workflow(stages=stages)


def load_workflow(project_dir: Path) -> list[Stage]:
    """The workflow's validated stages, for callers that want the list (the runner,
    the version snapshotter). Strict — delegates to `load_workflow_object`."""
    return load_workflow_object(project_dir).stages


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
    for f in list_stage_files(compiled_dir):
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
