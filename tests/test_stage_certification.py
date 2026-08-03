"""Tests for app/web/stage_test_views.build_certification — whether a stage's
plain-language summary has been checked against its code, and on how many cases."""
from __future__ import annotations

import pytest

from app import models as m
from app.web.stage_test_views import build_certification

_SCHEMA = {"columns": [{"name": "id", "type": "str"}], "primary_key": ["id"]}


def _stage(*, summary=None, type_="python_row_function", handle="function"):
    spec = {
        "id": "s", "name": "S", "type": type_,
        "inputs": [{"id": "up", "schema": _SCHEMA}],
        "output_schema": _SCHEMA,
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
    return m.parse_stage(spec)


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
    """One disagreement is enough: the summary and the code demonstrably differ,
    so the description is not a safe thing to review from."""
    cert = build_certification(_stage(summary="Does a thing."), _views(*statuses))
    assert cert.status == "failing"
    assert not cert.is_certified


def test_a_summary_with_no_tests_is_untested_not_certified():
    """The distinction the whole surface rests on — unverified must never render
    as verified."""
    cert = build_certification(_stage(summary="Does a thing."), [])
    assert cert.status == "untested"
    assert not cert.is_certified


def test_no_summary_is_unsummarised():
    assert build_certification(_stage(summary=None), []).status == "unsummarised"


def test_a_stage_whose_behaviour_is_not_code_is_not_applicable():
    """An enrich's keys are config a reviewer reads directly — there is no authored
    description standing between them and the behaviour, so nothing to certify."""
    stage = m.parse_stage({
        "id": "j", "name": "J", "type": "enrich",
        "inputs": [{"id": "a", "schema": _SCHEMA}, {"id": "b", "schema": _SCHEMA}],
        "output_schema": _SCHEMA,
        "join": {"keys": [{"left": "id", "right": "id"}]},
    })
    assert build_certification(stage, []).status == "n/a"


def test_a_frame_function_is_certifiable_too():
    """Certification is about a summary being checked, not about grain — a frame
    function carries both a summary and tests."""
    stage = _stage(summary="Ranks the rows.", type_="python_frame_function")
    assert build_certification(stage, _views("passed")).status == "certified"


def test_a_code_carrying_type_that_cannot_run_examples_is_untestable():
    """publish has a description no example can ever check, so `untestable`, not `n/a`."""
    stage = m.parse_stage({
        "id": "pub", "name": "Pub", "type": "publish",
        "inputs": [{"id": "up", "schema": _SCHEMA}],
        "publish": {"format": "csv"},
        "function": {"kind": "inline", "summary": "Writes one file per row.",
                     "code": "def transform(df, output_dir, trace_links):\n    return df"},
    })
    assert build_certification(stage, []).status == "untestable"


def test_filter_rows_with_a_description_and_no_examples_is_untested():
    """A filter CAN be exemplified, so the gap is that nobody wrote one, not that none can."""
    stage = _stage(summary="Keeps active rows.", type_="filter_rows", handle="filter")
    assert build_certification(stage, []).status == "untested"


def test_filter_rows_with_passing_examples_is_certified():
    stage = _stage(summary="Keeps active rows.", type_="filter_rows", handle="filter")
    assert build_certification(stage, _views("passed")).status == "certified"


def test_filter_rows_with_no_description_is_undescribed_not_untestable():
    """Missing a description outranks being untestable: without one the step cannot
    be reviewed at all, and that is the more actionable complaint."""
    stage = _stage(summary=None, type_="filter_rows", handle="filter")
    assert build_certification(stage, []).status == "unsummarised"


def test_publish_carries_a_function_so_it_is_not_n_a():
    """A publish stage's behaviour is authored code too, so a missing description
    there is a real gap rather than nothing to say."""
    stage = m.parse_stage({
        "id": "pub", "name": "Pub", "type": "publish",
        "inputs": [{"id": "up", "schema": _SCHEMA}],
        "publish": {"format": "csv"},
        "function": {"kind": "inline",
                     "code": "def transform(df, output_dir, trace_links):\n    return df"},
    })
    assert build_certification(stage, []).status == "unsummarised"
