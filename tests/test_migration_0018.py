"""0018 tags every stored workflow output `figure`, the one shape there was."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from app.models import parse_stage

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0018_a_stored_workflow_output_is_a_figure.py")


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0018", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scoreboard_totals(sid: str = "scoreboard_totals") -> dict[str, Any]:
    """texas_staar_and_income's stored `scoreboard_totals`, as version 20260825T134229 holds it."""
    return {
        "id": sid, "type": "aggregate", "cache": True, "compiler_notes": [],
        "description": "Count the districts ranked, and the tests behind them",
        "inputs": [{"id": "ranked_districts"}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "ranked_districts", "columns": [
                {"name": "tests_taken", "type": "float", "nullable": True}]}],
            "produces": [
                {"name": "district_count", "type": "int", "nullable": False,
                 "description": "Districts the ranking covers."},
                {"name": "tests_counted", "type": "float", "nullable": True,
                 "description": "Tests taken across those districts."},
            ],
        },
        "workflow_outputs": [
            {"slug": "districts_ranked", "label": "Districts ranked",
             "column": "district_count", "primary": True},
            {"slug": "tests_counted", "label": "Tests behind the ranking",
             "column": "tests_counted", "primary": False},
        ],
        "aggregate": {"group_by": [], "aggregations": [
            {"output_column": "district_count", "formula": "count"},
            {"output_column": "tests_counted", "formula": "sum",
             "value_column": "tests_taken"}]},
    }


def test_a_stored_output_becomes_a_figure_and_the_stage_parses():
    revision = _load_revision()
    document = {"stages": [_scoreboard_totals()]}

    assert revision.tag_stored_outputs(document) is True

    stage = document["stages"][0]
    assert [r["kind"] for r in stage["workflow_outputs"]] == ["figure", "figure"]
    figures = parse_stage(stage).list_published_figures()
    assert [(f.slug, f.column) for f in figures] == [
        ("districts_ranked", "district_count"), ("tests_counted", "tests_counted")]


def test_the_stage_is_otherwise_untouched():
    revision = _load_revision()
    document = {"stages": [_scoreboard_totals()]}

    revision.tag_stored_outputs(document)

    tagged = document["stages"][0]
    del tagged["workflow_outputs"]
    was = _scoreboard_totals()
    del was["workflow_outputs"]
    assert tagged == was


def test_every_stage_in_one_document_is_tagged():
    """`any` over a generator short-circuits, which would leave later stages untagged."""
    revision = _load_revision()
    document = {"stages": [_scoreboard_totals("first"), _scoreboard_totals("second")]}

    revision.tag_stored_outputs(document)

    assert all(r["kind"] == "figure" for stage in document["stages"]
               for r in stage["workflow_outputs"])


def test_a_document_publishing_nothing_is_left_alone():
    revision = _load_revision()
    document = {"stages": [{"id": "load", "type": "input_data", "description": "Load"}]}

    assert revision.tag_stored_outputs(document) is False


def test_a_replay_over_a_tagged_store_changes_nothing():
    revision = _load_revision()
    document = {"stages": [_scoreboard_totals()]}
    revision.tag_stored_outputs(document)
    tagged = copy.deepcopy(document)

    assert revision.tag_stored_outputs(document) is False
    assert document == tagged


def test_a_table_rule_is_left_as_it_is():
    revision = _load_revision()
    table = {"kind": "table", "slug": "districts", "label": "Districts",
             "columns": ["district_count"], "primary": True}
    stage = _scoreboard_totals()
    stage["workflow_outputs"] = [table]
    document = {"stages": [stage]}

    assert revision.tag_stored_outputs(document) is False
    assert document["stages"][0]["workflow_outputs"] == [table]


def test_an_output_naming_no_column_stops_the_migration():
    revision = _load_revision()
    stage = _scoreboard_totals()
    stage["workflow_outputs"] = [{"slug": "districts_ranked", "label": "Districts"}]

    with pytest.raises(ValueError, match="districts_ranked"):
        revision.tag_stored_outputs({"stages": [stage]})
