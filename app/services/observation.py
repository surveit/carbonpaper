"""Observed distinct values for the authoring surfaces: name-based lookup of one
input_data stage's column profile. Reading the file needs the runtime's input_data
path, and services must not import app.runtime — so the profiler is INJECTED:
a composition root allowed to import the runtime (app.web, app.mcp) calls
set_input_profiler(app.runtime.observation.profile_input_stage) at wiring time."""
from __future__ import annotations

from typing import Callable

from app.models import Stage, StageType
from app.models.observation import ColumnValueProfile, InputFrameProfile
from app.services import workspace
from app.services.errors import InputProfilerNotConfiguredError
from app.services.loader import load_workflow

# The injected capability: given an input_data Stage, load its bound file and
# report the frame's observed per-column value profiles. Opaque here — this
# module never learns how the file is read.
InputProfiler = Callable[[Stage], InputFrameProfile]

_input_profiler: InputProfiler | None = None


def set_input_profiler(profiler: InputProfiler) -> None:
    """Inject the frame profiler, once, from a composition root (like set_projects_dir)."""
    global _input_profiler
    _input_profiler = profiler


def observed_column_profile(
    project_id: str, stage_id: str, column: str
) -> ColumnValueProfile:
    """One column's observed values in an input stage's bound file. Every miss raises."""
    stage = _find_input_stage(project_id, stage_id)
    if _input_profiler is None:
        raise InputProfilerNotConfiguredError()
    profile = _input_profiler(stage)
    column_profile = profile.column_named(column)
    if column_profile is None:
        observed = ", ".join(c.name for c in profile.columns) or "(none)"
        raise ValueError(
            f"input '{stage_id}' of project '{project_id}' has no column "
            f"'{column}' — its file's observed columns: {observed}"
        )
    return column_profile


def _find_input_stage(project_id: str, stage_id: str) -> Stage:
    """The input_data stage called `stage_id`, or a loud error naming the real ones."""
    project_dir = workspace.resolve_project_dir(project_id)
    if not project_dir.is_dir():
        raise ValueError(f"no project '{project_id}' in the workspace")
    stages = load_workflow(project_dir)
    stage = next((s for s in stages if s.id == stage_id), None)
    if stage is None:
        input_ids = ", ".join(
            s.id for s in stages if s.type == StageType.input_data
        ) or "(none)"
        raise ValueError(
            f"no stage '{stage_id}' in project '{project_id}' — its input_data "
            f"stages: {input_ids}"
        )
    if stage.type != StageType.input_data:
        raise ValueError(
            f"stage '{stage_id}' is `{stage.type}`, not `input_data` — observed "
            "values come from an input stage's bound file"
        )
    return stage
