"""Tests for app/web/stage_test_views.build_certification — whether a stage's
plain-language summary has been checked against its code, and on how many cases."""
from __future__ import annotations

import pytest

from conftest import place_stage, reads_of

from app import models as m
from app.web.stage_test_views import build_certification

_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": True}]}
# Which signature form a type takes: the reshaping family replaces its input,
# the anchored family extends it.
_REPLACES_TYPES = {"python_frame_function", "aggregate", "union", "input_data", "report"}
# The two the model refuses an empty read set on: each is handed only what it reads.
_READS_THE_ROW_TYPES = {"filter_rows", "human_review_queue"}


def _signature_for(type_, schema):
    if type_ == "report":
        return {"form": "replaces"}
    if type_ in _REPLACES_TYPES:
        return {"form": "replaces", "produces": schema["columns"]}
    if type_ in _READS_THE_ROW_TYPES:
        return {"form": "extends", "reads": reads_of("up", schema["columns"])}
    return {"form": "extends"}



def _stage(*, summary=None, type_="python_row_function", handle="function"):
    spec = {
        "id": "s", "description": "S", "type": type_,
        "inputs": [{"id": "up"}],
        "signature": _signature_for(type_, _SCHEMA),
    }
    if handle == "function":
        spec["function"] = {
            "kind": "inline", "summary": summary,
            "code": "def transform(row):\n    return row",
        }
    else:
        spec["filter"] = {
            "summary": summary,
            "code": "def should_include(row):\n    return True",
        }
    return place_stage(m.parse_stage(spec))


def _views(*statuses):
    return [{"status": s} for s in statuses]


def test_all_passing_is_certified():
    cert = build_certification(_stage(summary="Does a thing."), _views("passed", "passed"))
    assert (cert.status, cert.passing, cert.total) == ("certified", 2, 2)
    assert cert.is_certified


@pytest.mark.parametrize("statuses", [
    ("passed", "mismatch"), ("error",), ("passed", "malformed"),
])
def test_any_non_passing_case_revokes_certification(statuses):
    cert = build_certification(_stage(summary="Does a thing."), _views(*statuses))
    assert cert.status == "failing"
    assert not cert.is_certified


def test_a_summary_with_no_tests_is_untested_not_certified():
    cert = build_certification(_stage(summary="Does a thing."), [])
    assert cert.status == "untested"
    assert not cert.is_certified


def test_no_summary_is_unsummarised():
    assert build_certification(_stage(summary=None), []).status == "unsummarised"


def test_a_stage_whose_behaviour_is_not_code_gets_no_badge():
    stage = m.parse_stage({
        "id": "j", "description": "J", "type": "enrich",
        "inputs": [{"id": "a"},
                   {"id": "b"}],
        "signature": {
            "form": "extends",
            "reads": [
                {"input": "a", "columns": _SCHEMA["columns"]},
                {"input": "b", "columns": _SCHEMA["columns"]},
            ],
            "adds": [{"name": "v", "type": "str", "nullable": True}],
        },
        "join": {"keys": [{"left": "id", "right": "id"}], "enrich_with": {"v": "v"}},
    })
    assert build_certification(place_stage(stage), []) is None


def test_a_frame_function_is_certifiable_too():
    stage = _stage(summary="Ranks the rows.", type_="python_frame_function")
    assert build_certification(stage, _views("passed")).status == "certified"


def test_a_code_carrying_type_that_cannot_run_examples_is_untestable():
    stage = m.parse_stage({
        "id": "pub", "description": "Pub", "type": "report",
        "signature": {"form": "replaces"},
        "inputs": [{"id": "up"}],
        "report": {"format": "csv"},
        "function": {"kind": "inline", "summary": "Writes one file per row.",
                     "code": "def transform(df, output_dir, citation_provider):\n    return df"},
    })
    assert build_certification(place_stage(stage), []).status == "untestable"


def test_filter_rows_with_a_description_and_no_examples_is_untested():
    stage = _stage(summary="Keeps active rows.", type_="filter_rows", handle="filter")
    assert build_certification(stage, []).status == "untested"


def test_filter_rows_with_passing_examples_is_certified():
    stage = _stage(summary="Keeps active rows.", type_="filter_rows", handle="filter")
    assert build_certification(stage, _views("passed")).status == "certified"


def test_filter_rows_with_no_description_is_undescribed_not_untestable():
    stage = _stage(summary=None, type_="filter_rows", handle="filter")
    assert build_certification(stage, []).status == "unsummarised"


def test_report_carries_a_function_so_it_still_gets_a_badge():
    stage = m.parse_stage({
        "id": "pub", "description": "Pub", "type": "report",
        "signature": {"form": "replaces"},
        "inputs": [{"id": "up"}],
        "report": {"format": "csv"},
        "function": {"kind": "inline",
                     "code": "def transform(df, output_dir, citation_provider):\n    return df"},
    })
    assert build_certification(place_stage(stage), []).status == "unsummarised"
