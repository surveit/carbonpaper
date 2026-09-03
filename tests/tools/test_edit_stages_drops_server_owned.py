"""The raw-JSON stage writers are the other half of the accommodation `SubmittedStage`
makes for add_stage: a patch is merged into the stored spec, so anything it names lands.
`tests` is what the stage panel's certification badge speaks for, and only
generate_stage_tests writes it."""
from __future__ import annotations

import json

import pytest

from app.services.loader import load_workflow
from app.models.stage import StageEdit
from app.tools.submitted_stage import edit_stages_reporting_drops
from stage_seed import add_stage

_TESTED_STAGE = "double"
_LOAD_SCHEMA = [{"name": "amount", "type": "float", "nullable": True}]


@pytest.fixture
def tour_project(projects_root):
    project_id = "seeded_examples"
    (projects_root / project_id).mkdir(parents=True, exist_ok=True)
    add_stage(project_id, {
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file", "params": {"format": "csv"}},
        "signature": {"form": "replaces", "produces": _LOAD_SCHEMA},
    })
    add_stage(project_id, {
        "id": _TESTED_STAGE, "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _LOAD_SCHEMA}],
            "adds": [{"name": "doubled", "type": "float", "nullable": True}],
        },
        "function": {"kind": "inline", "summary": "Doubles the amount.", "code":
            "def transform(row):\n"
            "    return {**row, 'doubled': row['amount'] * 2}"},
        "tests": [{"name": "doubles", "inputs": {"load": [{"amount": 2.0}]},
                   "expected": [{"amount": 2.0, "doubled": 4.0}]}],
    })
    return project_id


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
        "inputs": {"load": [{"amount": 9.0}]},
        "expected": None,
    }]

    reply = edit_stages_reporting_drops(tour_project, [
        StageEdit(stage_id=_TESTED_STAGE, changes_json=json.dumps({"tests": forged}))])

    assert reply.ok is True
    assert _stage(tour_project, _TESTED_STAGE).tests == seeded
    assert reply.warnings[0].startswith(f"`{_TESTED_STAGE}`: ignored server-owned fields: tests")


def test_a_patch_that_does_not_mention_tests_keeps_them(tour_project):
    """The strip runs on the PATCH: over the merged spec it would delete them on any edit."""
    seeded = _stage(tour_project, _TESTED_STAGE).tests

    reply = edit_stages_reporting_drops(tour_project, [
        StageEdit(stage_id=_TESTED_STAGE,
                  changes_json=json.dumps({"description": "Double the amount"}))])

    assert reply.ok is True
    assert reply.warnings == []
    assert _stage(tour_project, _TESTED_STAGE).tests == seeded
    assert _stage(tour_project, _TESTED_STAGE).description == "Double the amount"


def test_unparseable_changes_still_reach_the_service_for_its_own_error(tour_project):
    reply = edit_stages_reporting_drops(
        tour_project, [StageEdit(stage_id=_TESTED_STAGE, changes_json="{not json")])

    assert reply.ok is False
    assert any("JSON parse error" in issue for issue in reply.issues)
