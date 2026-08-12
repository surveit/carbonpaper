"""Load + save for a project's compiled stage files.

One JSON file per stage under `<project>/compiled/`. This module is the ONE place
that reaches the disk for them: nothing else globs `compiled/*.json`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from app.core.paths import repo_root
from app.models.workflow import Workflow, validate_workflow
from app.models.stage import Stage, parse_stage, stage_to_json
from app.models.stages.code import PythonFunction
from app.models.stages.starlark import StarlarkFunction
from app.core.utils import format_errors

from .errors import WorkflowLoadError

# Keys the loaders inject onto a loaded stage/schema dict for their own
# bookkeeping — never part of the spec, so a writer must strip them before
# validating or persisting (both spec models are `extra="forbid"`).
LOADER_BOOKKEEPING_KEYS: set[str] = {"_filename", "_order", "_error"}


@dataclass
class CompiledStageFile:
    filename: str
    stage: Stage | None = None
    issues: list[str] = field(default_factory=list)


def list_parsed_stages(entries: list[CompiledStageFile]) -> list[Stage]:
    return [entry.stage for entry in entries if entry.stage is not None]


def find_file_issues(entries: list[CompiledStageFile]) -> list[str]:
    return [f"{entry.filename}: {issue}" for entry in entries for issue in entry.issues]


def find_parsed_stage(entries: list[CompiledStageFile], stage_id: str) -> Stage | None:
    return next((s for s in list_parsed_stages(entries) if s.id == stage_id), None)


def list_stage_files(compiled_dir: Path) -> list[Path]:
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
            entry.stage = parse_stage(data)
        except ValidationError as err:
            entry.issues.extend(format_errors(err))
    return entries


def load_workflow_object(project_dir: Path) -> Workflow:
    compiled_dir = project_dir / "compiled"
    entries = load_compiled_dir(compiled_dir)
    issues = find_file_issues(entries)
    if not entries:
        issues.append(f"no compiled stage files found in {compiled_dir}")
    stages = [e.stage for e in entries if e.stage is not None]
    issues += validate_workflow(stages)
    if issues:
        raise WorkflowLoadError(compiled_dir, issues)
    return Workflow(stages=stages)


def load_workflow(project_dir: Path) -> list[Stage]:
    return load_workflow_object(project_dir).stages


# ─── Find & save ─────────────────────────────────────────────────────────────

def find_stage_file(compiled_dir: Path, stage_id: str) -> Path | None:
    for f in list_stage_files(compiled_dir):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("id") == stage_id:
            return f
    return None


def write_stage(path: Path, stage: Stage) -> None:
    path.write_text(stage_to_json(stage), encoding="utf-8")


# ─── Source & code reads ─────────────────────────────────────────────────────

def read_module_code(module_path: str) -> str | None:
    if not module_path:
        return None
    parts = module_path.split(".")
    candidate = repo_root() / Path(*parts).with_suffix(".py")
    if not candidate.exists():
        return None
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return None


def resolve_function_code(stage_def: Stage | None) -> str | None:
    fn = stage_def.find_authored_code_block() if stage_def else None
    if isinstance(fn, StarlarkFunction):
        return fn.code
    if not isinstance(fn, PythonFunction):
        return None
    if fn.kind == "module" and fn.module:
        return read_module_code(fn.module)
    if fn.kind == "inline":
        return fn.code
    return None
