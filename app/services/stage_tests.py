"""Running a project's authored stage tests, and the warning report that reads their result.

One of the three seams that reach `app.runtime` from `app.services`, which the
import-linter contract on `app.runtime` names.
"""
from __future__ import annotations

from app.models.compiler_warnings import CompilerWarningReport, find_workflow_compiler_warnings
from app.runtime.stage_tests import StageTestsReport, run_stage_tests as run_tests
from app.services import loader, workspace


def run_project_stage_tests(project_id: str, stage_id: str | None = None) -> StageTestsReport:
    return run_tests(_load_workflow(project_id), stage_id)


def find_project_compiler_warnings(project_id: str) -> CompilerWarningReport:
    stages = _load_workflow(project_id)
    return find_workflow_compiler_warnings(stages, run_tests(stages).count_failing_by_stage())


def _load_workflow(project_id: str):
    return loader.load_workflow(workspace.resolve_project_dir(project_id))
