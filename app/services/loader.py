"""Canonical loader for a methodology's compiled stage files.

One JSON file per stage under `<methodology>/compiled/`. This module is the
only place that reads the on-disk stage format; everything past it speaks
Stage objects. Two entry points:

  - load_compiled_dir: tolerant, per-file — for the viewer, which renders
    problems rather than crashing.
  - load_methodology_stages: strict — for the runner, which refuses to execute
    a DAG with any invalid stage or cross-stage issue.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from app.models.methodology import validate_methodology_stages
from app.models.schema import format_errors
from app.models.stage import Stage


@dataclass
class CompiledStageFile:
    """One compiled file: its parsed Stage (None if invalid) and any issues."""
    filename: str
    stage: Stage | None = None
    issues: list[str] = field(default_factory=list)


class MethodologyLoadError(Exception):
    """The compiled DAG failed validation; `issues` lists every problem found."""

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
            entry.stage = Stage.model_validate(data)
        except ValidationError as err:
            entry.issues.extend(format_errors(err))
    return entries


def load_methodology_stages(methodology_dir: Path) -> list[Stage]:
    compiled_dir = methodology_dir / "compiled"
    entries = load_compiled_dir(compiled_dir)
    issues = [f"{e.filename}: {i}" for e in entries for i in e.issues]
    if not entries:
        issues.append(f"no compiled stage files found in {compiled_dir}")
    stages = [e.stage for e in entries if e.stage is not None]
    issues += validate_methodology_stages(stages)
    if issues:
        raise MethodologyLoadError(compiled_dir, issues)
    return stages
