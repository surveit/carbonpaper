"""The authoring boundary refuses to WRITE a code-carrying stage whose description
is not fully submitted.

Enforced on the write path, not on the model or the shared draft validator — a
stage stored before the field existed, or frozen in a version, must still load.
"""
from __future__ import annotations

import json

import pytest

from app.services.stage_edit import add_stage_spec, edit_stage_spec

_SCHEMA = {"columns": [{"name": "id", "type": "str"}], "primary_key": ["id"]}
_CODE = "def transform(row):\n    return row\n"


def _spec(stage_id="tag", **function_extra):
    return {
        "id": stage_id, "name": "Tag", "type": "python_row_function",
        "inputs": [{"id": "src", "schema": _SCHEMA}],
        "output_schema": _SCHEMA,
        "function": {"kind": "inline", "code": _CODE,
                     "corner_cases": [], **function_extra},
    }


def _source_spec():
    return {
        "id": "src", "name": "Source", "type": "input_data",
        "connector": {"kind": "file", "params": {"format": "csv"}},
        "output_schema": _SCHEMA,
    }


@pytest.fixture
def project(tmp_path):
    (tmp_path / "compiled").mkdir()
    assert add_stage_spec(tmp_path, json.dumps(_source_spec())).ok
    return tmp_path


def test_adding_a_code_stage_without_a_summary_is_refused(project):
    result = add_stage_spec(project, json.dumps(_spec()))
    assert not result.ok
    assert any("summary` is required" in issue for issue in result.issues)


def test_adding_a_code_stage_with_a_summary_is_accepted(project):
    result = add_stage_spec(project, json.dumps(_spec(summary="Passes rows through.")))
    assert result.ok, result.issues


def test_a_blank_summary_does_not_satisfy_the_gate(project):
    """Whitespace is not a description, and an agent under pressure will try it."""
    result = add_stage_spec(project, json.dumps(_spec(summary="   ")))
    assert not result.ok
    assert any("summary` is required" in issue for issue in result.issues)


def test_editing_a_summary_away_is_refused(project):
    """The gate covers edits, not just creation — otherwise a description can be
    removed from a stage that already passed."""
    assert add_stage_spec(project, json.dumps(_spec(summary="Passes rows through."))).ok
    result = edit_stage_spec(project, "tag", json.dumps(_spec()))
    assert not result.ok
    assert any("summary` is required" in issue for issue in result.issues)


def test_a_config_only_stage_needs_no_summary(project):
    """A join's keys are config a reviewer reads directly — there is no authored
    code for prose to stand in for."""
    result = add_stage_spec(project, json.dumps({
        "id": "j", "name": "J", "type": "join",
        "inputs": [{"id": "src", "schema": _SCHEMA}, {"id": "src2", "schema": _SCHEMA}],
        "output_schema": _SCHEMA,
        "join": {"type": "inner", "keys": [{"left": "id", "right": "id"}]},
    }))
    # Refused for the missing `src2` edge, never for a missing summary.
    assert not any("summary" in issue for issue in result.issues)


def test_an_empty_corner_case_list_is_a_valid_answer(project):
    """A step may genuinely have no awkward inputs, and requiring a non-empty list
    would make an agent pad it and invent behaviour."""
    result = add_stage_spec(project, json.dumps(_spec(summary="Passes rows through.")))
    assert result.ok, result.issues


def test_omitting_corner_cases_entirely_is_refused(project):
    """`[]` and an absent key are the two states a reviewer most needs told apart:
    "none" versus "never considered". Only the first may be written."""
    spec = _spec(summary="Passes rows through.")
    del spec["function"]["corner_cases"]
    result = add_stage_spec(project, json.dumps(spec))
    assert not result.ok
    assert any("corner_cases` must be submitted" in issue for issue in result.issues)


def test_stated_corner_cases_round_trip(project):
    result = add_stage_spec(project, json.dumps(_spec(
        summary="Passes rows through.",
        corner_cases=[{"case": "`id` is blank", "expected": "the step fails"}])))
    assert result.ok, result.issues
