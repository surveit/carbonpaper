"""add_stage takes a LIST: these pin the batch outcome model — order resolved, partial
success kept, an unorderable batch refused whole. Every assertion checks the STORED
workflow, not just the payload, since "added" is worth nothing if it is not on disk."""
from __future__ import annotations

import asyncio

import pytest
from app.services import workspace

_CLAIM = {"columns": [
    {"name": "claim_id", "type": "str", "nullable": False},
    {"name": "amount", "type": "float", "nullable": False},
]}
_CLEANED = {"columns": [
    {"name": "claim_id", "type": "str", "nullable": False},
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "cleaned", "type": "bool", "nullable": False},
]}

_LOAD = {
    "id": "load", "description": "Load claims", "type": "input_data",
    "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _CLAIM["columns"]},
}
_CLEAN = {
    "id": "clean", "description": "Clean", "type": "python_row_function",
    "inputs": [{"id": "load"}],
    "function": {"kind": "inline", "summary": "Test fixture step.", "corner_cases": [],
                 "code": "def transform(row):\n    return {**row, 'cleaned': True}\n"},
    "signature": {
        "form": "extends",
        "reads": [{"input": "load", "columns": _CLAIM["columns"]}],
        "adds": [{"name": "cleaned", "type": "bool", "nullable": False}],
    },
}
# Refused by Stage: an llm_transform's signature must read exactly what its
# template injects, and this reads nothing. Real validation, not a fixture trick.
_SCORE_UNADDITIVE = {
    "id": "score", "description": "Score", "type": "llm_transform",
    "inputs": [{"id": "clean"}],
    "signature": {"form": "extends",
                  "adds": [{"name": "verdict", "type": "str", "nullable": True}]},
    "llm": {"model": "claude-haiku-4-5", "prompt_data_template": "judge {amount}"},
}
_RANK = {
    "id": "rank", "description": "Rank", "type": "python_row_function",
    "inputs": [{"id": "score"}],
    "function": {"kind": "inline", "summary": "Test fixture step.", "corner_cases": [],
                 "code": "def transform(row):\n    return {**row, 'rank': 1}\n"},
    "signature": {
        "form": "extends",
        "reads": [
            {
                "input": "score",
                "columns": [{"name": "verdict", "type": "str", "nullable": True}],
            },
        ],
        "adds": [{"name": "rank", "type": "int", "nullable": False}],
    },
}
_REPORT = {
    "id": "report", "description": "Report", "type": "python_row_function",
    "inputs": [{"id": "rank"}],
    "function": {"kind": "inline", "summary": "Test fixture step.", "corner_cases": [],
                 "code": "def transform(row):\n    return {**row, 'note': 'x'}\n"},
    "signature": {
        "form": "extends",
        "reads": [
            {
                "input": "rank",
                "columns": [{"name": "rank", "type": "int", "nullable": False}],
            },
        ],
        "adds": [{"name": "note", "type": "str", "nullable": False}],
    },
}


def _call_add_stage(project_id, stages):
    from app.mcp import server

    _content, result = asyncio.run(
        server.mcp.call_tool("add_stage", {"project_id": project_id, "stages": stages})
    )
    return result


def _list_stored_stage_ids(project_id) -> set[str]:
    return {p.stem for p in (workspace.projects_dir() / project_id / "compiled").glob("*.json")}


@pytest.fixture
def project(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    created = server.create_project(name="trail", document="Follow the filings.")
    return created.id


def test_a_batch_submitted_in_reverse_dependency_order_is_sorted_and_stored(project):
    result = _call_add_stage(project, [_CLEAN, _LOAD])

    assert result["ok"] is True, result["issues"]
    assert result["added"] == ["load", "clean"], "stored in dependency order"
    assert _list_stored_stage_ids(project) == {"load", "clean"}


def test_one_failure_keeps_the_independents_and_skips_only_its_dependency_cone(project):
    result = _call_add_stage(project, [_REPORT, _RANK, _SCORE_UNADDITIVE, _CLEAN, _LOAD])

    assert result["ok"] is False
    assert result["added"] == ["load", "clean"]
    [failure] = result["failed"]
    assert failure["id"] == "score"
    assert any("does not read it" in issue for issue in failure["issues"])
    assert result["skipped"] == [
        {"id": "rank", "because": "inputs from score"},
        # transitive, and named by its NEAREST cause rather than the root
        {"id": "report", "because": "inputs from rank"},
    ]
    assert _list_stored_stage_ids(project) == {"load", "clean"}, "no skipped stage was written"


def test_the_flattened_issues_still_carry_every_failure(project):
    result = _call_add_stage(project, [_LOAD, _CLEAN, _SCORE_UNADDITIVE])

    assert result["issues"] == result["failed"][0]["issues"]


def test_a_cycle_among_the_submitted_stages_refuses_the_whole_batch(project):
    a = {**_CLEAN, "id": "a", "inputs": [{"id": "b"}]}
    b = {**_CLEAN, "id": "b", "inputs": [{"id": "a"}]}

    result = _call_add_stage(project, [_LOAD, a, b])

    assert result["ok"] is False
    assert result["added"] == [] and result["failed"] == [] and result["skipped"] == []
    [issue] = result["issues"]
    assert "cycle" in issue and "a" in issue and "b" in issue
    assert _list_stored_stage_ids(project) == set(), "not even the valid load was written"


def test_two_stages_sharing_an_id_refuse_the_whole_batch(project):
    """Which of the two `clean` inputs from has no answer, so writing either picks arbitrarily."""
    result = _call_add_stage(project, [_LOAD, {**_LOAD, "description": "Load again"}])

    assert result["ok"] is False and result["added"] == []
    assert any("duplicate" in issue for issue in result["issues"])
    assert _list_stored_stage_ids(project) == set()


def test_a_one_element_list_refuses_exactly_as_the_singular_call_did(project):
    _call_add_stage(project, [_LOAD, _CLEAN])

    result = _call_add_stage(project, [_SCORE_UNADDITIVE])

    assert result["ok"] is False
    assert any("does not read it" in issue for issue in result["issues"])
    assert _list_stored_stage_ids(project) == {"load", "clean"}


def test_a_stage_added_earlier_in_the_batch_satisfies_a_later_stage_edge(project):
    result = _call_add_stage(project, [_LOAD, _CLEAN, _RANK])

    assert result["added"] == ["load", "clean"]
    [failure] = result["failed"]
    assert failure["id"] == "rank", "its input `score` is in neither the batch nor the workflow"


def test_a_json_string_is_still_accepted_for_the_list(project):
    """FastMCP's pre_parse_json decodes a string sent for a non-str parameter."""
    import json

    result = _call_add_stage(project, json.dumps([_LOAD, _CLEAN]))

    assert result["added"] == ["load", "clean"]
    assert _list_stored_stage_ids(project) == {"load", "clean"}
