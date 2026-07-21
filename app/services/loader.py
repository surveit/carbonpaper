"""Canonical load + save for a project's compiled stage files.

One JSON file per stage under `<project>/compiled/`. This module is the ONE
place that knows the on-disk stage format — both directions. Everything past it
speaks `Stage` objects; nothing else should call `model_dump_json` on a stage or
glob `compiled/*.json`, so a format change (or the planned rename) touches only
this file.

Read:
  - load_compiled_dir: tolerant, per-file — for the viewer, which renders
    problems rather than crashing.
  - load_workflow_object: strict — the whole workflow as one in-memory
    `Workflow` object; raises on any invalid stage or cross-stage issue.
  - load_workflow: strict — the same, returning just the `list[Stage]` for
    callers that want the stages (the runner, the version snapshotter).

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

from app.core.models.workflow import (
    Workflow,
    detect_cycle,
    group_dangling_inputs,
    validate_unique_ids,
)
from app.core.models.schema import format_errors
from app.core.models.stage import Stage


@dataclass
class CompiledStageFile:
    """One compiled file: its parsed Stage (None if invalid) and any issues.
    `raw_id` is the file's `id` field read straight off the parsed JSON, kept even
    when the stage fails Stage validation (most schema errors leave `id` itself
    fine) — it's what lets a downstream dangling-input cascade be matched back to
    the file that caused it. None when the file wasn't even valid JSON, or wasn't
    a JSON object, or had no `id`."""
    filename: str
    stage: Stage | None = None
    issues: list[str] = field(default_factory=list)
    raw_id: str | None = None


class WorkflowLoadError(Exception):
    """A stored workflow failed validation; `issues` lists every problem found.
    `source` names where the workflow was read from — a compiled/ directory, or
    a version document in the store."""

    def __init__(self, source: Path | str, issues: list[str]):
        self.issues = issues
        super().__init__(
            f"{source}: {len(issues)} validation issue(s):\n  "
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
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            entry.raw_id = data["id"]
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
    (the list accessor) delegates here.

    A stage dropped by a bad file (see CompiledStageFile.raw_id) leaves every
    downstream stage that names it as an input dangling — DE-CASCADED here rather
    than reported once per downstream consumer: `group_dangling_inputs` groups
    those by the missing id, and a group whose id matches a broken file's raw_id is
    folded into that file's OWN issue line (the file's error already explains why
    the id is gone) instead of appended as a separate problem. A group with no
    matching file — a plain typo'd id, nothing failed to load — still reports as
    one line, not one per consumer. The result: a handful of root causes reads as
    a handful of lines, not one line per cascaded symptom."""
    compiled_dir = project_dir / "compiled"
    entries = load_compiled_dir(compiled_dir)
    stages = [e.stage for e in entries if e.stage is not None]
    dangling_by_upstream = {g.upstream: g for g in group_dangling_inputs(stages)}

    issues: list[str] = []
    for e in entries:
        if not e.issues:
            continue
        line = f"{e.filename}: {'; '.join(e.issues)}"
        group = dangling_by_upstream.pop(e.raw_id, None) if e.raw_id else None
        if group is not None:
            line += f" ({group.as_cascade_note()})"
        issues.append(line)
    if not entries:
        issues.append(f"no compiled stage files found in {compiled_dir}")
    issues += validate_unique_ids(stages)
    # Whatever's left named no broken file — an id that's simply wrong.
    issues += [g.as_issue() for g in dangling_by_upstream.values()]
    issues += detect_cycle(stages)

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
