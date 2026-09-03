"""The authoring boundary refuses to WRITE a code-carrying stage whose description
is not fully submitted.

Enforced on the write path, not on the model or the shared draft validator — a
stage stored before the field existed, or frozen in a version, must still load.
"""
from __future__ import annotations

import json

import pytest

from app.models.stages.code import SUMMARY_DESCRIPTION, SUMMARY_MAX_CHARS
from app.services.code_approval import approve_code_execution
from app.services.stage_edit import open_working_copy, add_stage_spec, edit_stage_spec

_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": True}]}
_CODE = "def transform(row):\n    return row\n"


def _spec(stage_id="tag", **function_extra):
    return {
        "id": stage_id, "description": "Tag", "type": "python_row_function",
        "inputs": [{"id": "src"}],
        "signature": {"form": "extends", "reads": [{"input": "src", "columns": _SCHEMA["columns"]}]},
        "function": {"kind": "inline", "code": _CODE,
                     "corner_cases": [], **function_extra},
    }


def _source_spec():
    return {
        "id": "src", "description": "Source", "type": "input_data",
        "connector": {"kind": "file", "params": {"format": "csv"}},
        "signature": {"form": "replaces", "produces": _SCHEMA["columns"]},
    }


@pytest.fixture
def project(tmp_path):
    name = tmp_path.name
    # These stages are python_row_function on purpose — the rule under test binds
    # code-carrying types, and an unapproved project is refused before reaching it.
    approve_code_execution(name, "fixture: the rule under test is about code stages")
    assert add_stage_spec(open_working_copy(name), json.dumps(_source_spec())).ok
    return name


def test_adding_a_code_stage_without_a_summary_is_refused(project):
    result = add_stage_spec(open_working_copy(project), json.dumps(_spec()))
    assert not result.ok
    assert any("summary` is required" in issue for issue in result.issues)


def test_adding_a_code_stage_with_a_summary_is_accepted(project):
    result = add_stage_spec(open_working_copy(project), json.dumps(_spec(summary="Passes rows through.")))
    assert result.ok, result.issues


def test_a_blank_summary_does_not_satisfy_the_gate(project):
    result = add_stage_spec(open_working_copy(project), json.dumps(_spec(summary="   ")))
    assert not result.ok
    assert any("summary` is required" in issue for issue in result.issues)


def test_editing_a_summary_away_is_refused(project):
    assert add_stage_spec(open_working_copy(project), json.dumps(_spec(summary="Passes rows through."))).ok
    result = edit_stage_spec(open_working_copy(project), "tag", json.dumps(_spec()))
    assert not result.ok
    assert any("summary` is required" in issue for issue in result.issues)


def test_a_config_only_stage_needs_no_summary(project):
    result = add_stage_spec(open_working_copy(project), json.dumps({
        "id": "j", "description": "J", "type": "enrich",
        "inputs": [{"id": "src"},
                   {"id": "src2"}],
        "signature": {
            "form": "extends",
            "reads": [
                {"input": "src", "columns": _SCHEMA["columns"]},
                {"input": "src2", "columns": _SCHEMA["columns"]},
            ],
            "adds": [{"name": "v", "type": "str", "nullable": True}],
        },
        "join": {"keys": [{"left": "id", "right": "id"}], "enrich_with": {"v": "v"}},
    }))
    # Refused for the missing `src2` edge, never for a missing summary.
    assert not any("summary" in issue for issue in result.issues)


def test_an_empty_corner_case_list_is_a_valid_answer(project):
    result = add_stage_spec(open_working_copy(project), json.dumps(_spec(summary="Passes rows through.")))
    assert result.ok, result.issues


def test_omitting_corner_cases_entirely_is_refused(project):
    """`[]` says "none"; an absent key says "never considered". Only the first may be written."""
    spec = _spec(summary="Passes rows through.")
    del spec["function"]["corner_cases"]
    result = add_stage_spec(open_working_copy(project), json.dumps(spec))
    assert not result.ok
    assert any("corner_cases` must be submitted" in issue for issue in result.issues)


def test_stated_corner_cases_round_trip(project):
    result = add_stage_spec(open_working_copy(project), json.dumps(_spec(
        summary="Passes rows through.",
        corner_cases=[{"case": "`id` is blank", "expected": "the step fails"}])))
    assert result.ok, result.issues



# ── the summary hard limit ───────────────────────────────────────────────────
def test_the_field_description_states_the_limit_this_path_refuses_on():
    # SUMMARY_DESCRIPTION is what an authoring client is shown before it writes.
    assert str(SUMMARY_MAX_CHARS) in SUMMARY_DESCRIPTION


def test_a_summary_over_the_limit_is_refused(project):
    result = add_stage_spec(open_working_copy(project), json.dumps(_spec(summary="x" * (SUMMARY_MAX_CHARS + 1))))
    assert not result.ok
    assert any(str(SUMMARY_MAX_CHARS) in issue for issue in result.issues)


def test_a_summary_exactly_at_the_limit_is_accepted(project):
    result = add_stage_spec(open_working_copy(project), json.dumps(_spec(summary="x" * SUMMARY_MAX_CHARS)))
    assert result.ok, result.issues
