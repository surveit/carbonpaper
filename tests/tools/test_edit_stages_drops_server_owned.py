"""The raw-JSON stage writers are the other half of the accommodation `SubmittedStage`
makes for add_stage: a patch is merged into the stored spec, so anything it names lands.
`tests` is what the stage panel's certification badge speaks for, and only
generate_stage_tests writes it."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.loader import load_workflow
from app.services.project import WorkflowFile, import_project
from app.models.stage import StageEdit
from app.services import stage_edit
from app.tools.submitted_stage import edit_stages_reporting_drops

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "app" / "seeds" / "data" / "tutorial_lobbying_triage.json"
)
# The one stage of the tour's workflow whose type may carry tests, and the examples
# seeded on it — the count is the fixture's, read back rather than restated.
_TESTED_STAGE = "clean_filings"


@pytest.fixture
def tour_project(projects_root):
    return import_project(
        WorkflowFile.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))
    )


def _stage(project_id: str, stage_id: str):
    return next(
        s for s in load_workflow(project_id)
        if s.id == stage_id
    )


def test_a_patch_carrying_tests_leaves_the_stored_ones_untouched(tour_project):
    seeded = _stage(tour_project, _TESTED_STAGE).tests
    assert seeded, "the fixture is supposed to ship examples on this stage"
    forged = [{
        "name": "written by the client, not the generator",
        "inputs": {"lobbying_filings": [{"filing_id": "X"}]},
        "expected": None,
    }]

    reply = edit_stages_reporting_drops(stage_edit.open_working_copy(tour_project), [
        StageEdit(stage_id=_TESTED_STAGE, changes_json=json.dumps({"tests": forged}))])

    assert reply.ok is True
    assert _stage(tour_project, _TESTED_STAGE).tests == seeded
    assert reply.warnings[0].startswith(f"`{_TESTED_STAGE}`: ignored server-owned fields: tests")


def test_a_patch_that_does_not_mention_tests_keeps_them(tour_project):
    """The strip runs on the PATCH: over the merged spec it would delete them on any edit."""
    seeded = _stage(tour_project, _TESTED_STAGE).tests

    reply = edit_stages_reporting_drops(stage_edit.open_working_copy(tour_project), [
        StageEdit(stage_id=_TESTED_STAGE,
                  changes_json=json.dumps({"description": "Check each filing"}))])

    assert reply.ok is True
    assert reply.warnings == []
    assert _stage(tour_project, _TESTED_STAGE).tests == seeded
    assert _stage(tour_project, _TESTED_STAGE).description == "Check each filing"


def test_unparseable_changes_still_reach_the_service_for_its_own_error(tour_project):
    reply = edit_stages_reporting_drops(stage_edit.open_working_copy(tour_project), [StageEdit(stage_id=_TESTED_STAGE, changes_json="{not json")])

    assert reply.ok is False
    assert any("JSON parse error" in issue for issue in reply.issues)
