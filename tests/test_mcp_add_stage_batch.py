"""add_stage takes a LIST: these pin the batch outcome model — order derived, partial
success kept, an unorderable batch refused whole. Every assertion checks the STORED
workflow, not just the payload, since "added" is worth nothing if it is not on disk."""
from __future__ import annotations

import asyncio

import pytest

_CLAIM = {"columns": [
    {"name": "claim_id", "type": "str", "nullable": False},
    {"name": "amount", "type": "float", "nullable": False},
]}
_CLEANED = {"columns": [
    {"name": "claim_id", "type": "str", "nullable": False},
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "cleaned", "type": "bool", "nullable": False},
], "primary_key": ["claim_id"]}

_LOAD = {
    "id": "load", "name": "Load claims", "type": "input_data",
    "connector": {"kind": "file"}, "output_schema": _CLAIM,
}
_CLEAN = {
    "id": "clean", "name": "Clean", "type": "python_row_function",
    "inputs": [{"id": "load", "schema": _CLAIM}],
    "function": {"kind": "inline", "summary": "Test fixture step.",
                 "code": "def transform(row):\n    return {**row, 'cleaned': True}\n"},
    "output_schema": _CLEANED,
}
# Refused by Stage: an llm_transform must be additive and 1:1, and this drops
# `amount`. The failure is real validation, not a fixture trick.
_SCORE_UNADDITIVE = {
    "id": "score", "name": "Score", "type": "llm_transform",
    "inputs": [{"id": "clean", "schema": _CLEANED}],
    "output_schema": {"columns": [{"name": "verdict", "type": "str", "nullable": True}]},
    "llm": {"prompt_data_template": "judge {amount}"},
}
_RANK = {
    "id": "rank", "name": "Rank", "type": "python_row_function",
    "inputs": [{"id": "score", "schema": {
        "columns": [{"name": "verdict", "type": "str", "nullable": True}]}}],
    "function": {"kind": "inline", "summary": "Test fixture step.",
                 "code": "def transform(row):\n    return {**row, 'rank': 1}\n"},
    "output_schema": {"columns": [
        {"name": "verdict", "type": "str", "nullable": True},
        {"name": "rank", "type": "int", "nullable": False},
    ]},
}
_REPORT = {
    "id": "report", "name": "Report", "type": "python_row_function",
    "inputs": [{"id": "rank", "schema": {
        "columns": [{"name": "rank", "type": "int", "nullable": False}]}}],
    "function": {"kind": "inline", "summary": "Test fixture step.",
                 "code": "def transform(row):\n    return {**row, 'note': 'x'}\n"},
    "output_schema": {"columns": [
        {"name": "rank", "type": "int", "nullable": False},
        {"name": "note", "type": "str", "nullable": False},
    ]},
}


def _call_add_stage(stages):
    from app.mcp import server

    _content, result = asyncio.run(
        server.mcp.call_tool("add_stage", {"project_id": "trail", "stages": stages})
    )
    return result


def _list_stored_stage_ids(tmp_path) -> set[str]:
    return {p.stem for p in (tmp_path / "trail" / "compiled").glob("*.json")}


@pytest.fixture
def project(tmp_path, monkeypatch):
    from app.mcp import server
    from app.services import workspace

    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    server.create_project(name="trail", document="Follow the filings.")
    return tmp_path


def test_a_batch_submitted_in_reverse_dependency_order_is_sorted_and_stored(project):
    """The client is not asked to topo-sort its own plan: a stage may name a stage
    submitted LATER in the same list."""
    result = _call_add_stage([_CLEAN, _LOAD])

    assert result["ok"] is True, result["issues"]
    assert result["added"] == ["load", "clean"], "stored in dependency order"
    assert _list_stored_stage_ids(project) == {"load", "clean"}


def test_one_failure_keeps_the_independents_and_skips_only_its_dependency_cone(project):
    """The point of partial success: a refusal three stages in does not throw away
    the two that validated, and does not waste a round-trip failing the stages that
    could only have failed on the input now missing."""
    result = _call_add_stage([_REPORT, _RANK, _SCORE_UNADDITIVE, _CLEAN, _LOAD])

    assert result["ok"] is False
    assert result["added"] == ["load", "clean"]
    [failure] = result["failed"]
    assert failure["id"] == "score"
    assert any("1:1" in issue for issue in failure["issues"])
    assert result["skipped"] == [
        {"id": "rank", "because": "inputs from score"},
        # transitive, and named by its NEAREST cause rather than the root
        {"id": "report", "because": "inputs from rank"},
    ]
    assert _list_stored_stage_ids(project) == {"load", "clean"}, "no skipped stage was written"


def test_the_flattened_issues_still_carry_every_failure(project):
    """`ok`/`issues` is the refusal channel the instructions tell a client to watch;
    the per-stage detail is additive, not a replacement for it."""
    result = _call_add_stage([_LOAD, _CLEAN, _SCORE_UNADDITIVE])

    assert result["issues"] == result["failed"][0]["issues"]


def test_a_cycle_among_the_submitted_stages_refuses_the_whole_batch(project):
    """Unorderable, so nothing is attempted — and the message names the cycle
    rather than reporting a stage-by-stage failure that would misdescribe it."""
    a = {**_CLEAN, "id": "a", "inputs": [{"id": "b", "schema": _CLAIM}]}
    b = {**_CLEAN, "id": "b", "inputs": [{"id": "a", "schema": _CLAIM}]}

    result = _call_add_stage([_LOAD, a, b])

    assert result["ok"] is False
    assert result["added"] == [] and result["failed"] == [] and result["skipped"] == []
    [issue] = result["issues"]
    assert "cycle" in issue and "a" in issue and "b" in issue
    assert _list_stored_stage_ids(project) == set(), "not even the valid load was written"


def test_two_stages_sharing_an_id_refuse_the_whole_batch(project):
    """Also unorderable: which of the two is the stage `clean` inputs from has no
    answer. Writing one and failing the other would pick one arbitrarily."""
    result = _call_add_stage([_LOAD, {**_LOAD, "name": "Load again"}])

    assert result["ok"] is False and result["added"] == []
    assert any("duplicate" in issue for issue in result["issues"])
    assert _list_stored_stage_ids(project) == set()


def test_a_one_element_list_refuses_exactly_as_the_singular_call_did(project):
    """The singular case is a list of one — the refusal stays on the {ok, issues}
    payload channel, and nothing is written."""
    _call_add_stage([_LOAD, _CLEAN])

    result = _call_add_stage([_SCORE_UNADDITIVE])

    assert result["ok"] is False
    assert any("1:1" in issue for issue in result["issues"])
    assert _list_stored_stage_ids(project) == {"load", "clean"}


def test_a_stage_added_earlier_in_the_batch_satisfies_a_later_stage_edge(project):
    """The whole reason a batch is more than a loop of refusals: `clean`'s declared
    input schema is validated against `load`'s output_schema, and `load` only exists
    because this same call put it there."""
    result = _call_add_stage([_LOAD, _CLEAN, _RANK])

    assert result["added"] == ["load", "clean"]
    [failure] = result["failed"]
    assert failure["id"] == "rank", "its input `score` is in neither the batch nor the workflow"


def test_a_json_string_is_still_accepted_for_the_list(project):
    """FastMCP's pre_parse_json decodes a string sent for a non-str parameter, so a
    client that serialises its argument keeps working now that it is an array."""
    import json

    result = _call_add_stage(json.dumps([_LOAD, _CLEAN]))

    assert result["added"] == ["load", "clean"]
    assert _list_stored_stage_ids(project) == {"load", "clean"}
