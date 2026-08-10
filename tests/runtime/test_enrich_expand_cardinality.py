"""enrich vs expand: the same LEFT join, differing only in the cardinality
each permits — enrich VERIFIES the reference is unique on the key and fails the
run when it is not; expand fans out instead."""
from __future__ import annotations

import pandas as pd
import pytest

from app.models import Stage, parse_stage
from app.runtime.stages.join import handle_enrich, handle_expand
from conftest import rows_of, as_inputs, make_run_context

_SUBJECT = {"columns": [{"name": "x", "type": "int", "nullable": True}]}
_REFERENCE = {"columns": [{"name": "x", "type": "int", "nullable": True}, {"name": "z", "type": "str", "nullable": True}]}


def _join_stage(stage_type: str) -> Stage:
    return parse_stage({
        "id": "m", "description": "Join", "type": stage_type,
        "inputs": [{"id": "subject", "schema": _SUBJECT},
                   {"id": "reference", "schema": _REFERENCE}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "subject", "columns": [
                          {"name": "x", "type": "int", "nullable": True}]},
                      {"input": "reference", "columns": [
                          {"name": "x", "type": "int", "nullable": True}]}],
            "adds": [{"name": "z", "type": "str", "nullable": True}]},
        "join": {"keys": [{"left": "x", "right": "x"}], "enrich_with": {"z": "z"}},
    })


def _run(handler, stage_type: str, reference: pd.DataFrame) -> pd.DataFrame:
    return handler(
        _join_stage(stage_type),
        as_inputs({"subject": pd.DataFrame({"x": [1, 2]}), "reference": reference}),
        make_run_context(),
    )


def test_enrich_keeps_an_unmatched_subject_row_carrying_nulls():
    out = _run(handle_enrich, "enrich", pd.DataFrame({"x": [1], "z": ["a"]}))
    assert list(rows_of(out)["x"]) == [1, 2]
    assert rows_of(out)["z"].tolist()[0] == "a" and pd.isna(rows_of(out)["z"].tolist()[1])


def test_enrich_preserves_subject_order_even_when_the_keys_are_unsorted():
    # The type comment claims enrich leaves row count AND order unchanged. Sorted
    # subject keys would pass either way, so the keys here are deliberately out of
    # order and the reference is in a different order again.
    stage = _join_stage("enrich")
    subject = pd.DataFrame({"x": [30, 10, 20, 10]})
    reference = pd.DataFrame({"x": [10, 20, 30], "z": ["ten", "twenty", "thirty"]})
    out = handle_enrich(stage, as_inputs({"subject": subject, "reference": reference}), make_run_context())
    assert list(rows_of(out)["x"]) == [30, 10, 20, 10]
    assert list(rows_of(out)["z"]) == ["thirty", "ten", "twenty", "ten"]


def test_enrich_fails_loudly_when_the_reference_repeats_a_key():
    reference = pd.DataFrame({"x": [1, 1], "z": ["a", "b"]})
    with pytest.raises(ValueError) as err:
        _run(handle_enrich, "enrich", reference)
    msg = str(err.value)
    assert "stage 'm'" in msg and "'reference'" in msg
    assert "x=x" in msg  # the key pairs
    assert "expand" in msg  # the fix when the fan-out is intended
    assert "aggregating" in msg and "narrowing" in msg


def test_expand_fans_a_subject_row_out_over_every_matching_reference_row():
    out = _run(handle_expand, "expand", pd.DataFrame({"x": [1, 1], "z": ["a", "b"]}))
    assert list(rows_of(out)["x"]) == [1, 1, 2]
    assert rows_of(out)["z"].tolist()[:2] == ["a", "b"] and pd.isna(rows_of(out)["z"].tolist()[2])


def test_output_is_subject_columns_plus_enrich_with_only():
    # `extra` is not brought, so the handler narrows the reference before merging.
    out = handle_expand(
        _join_stage("expand"),
        as_inputs({"subject": pd.DataFrame({"x": [1]}),
         "reference": pd.DataFrame({"x": [1], "z": ["a"], "extra": ["noise"]})}),
        make_run_context(),
    )
    assert list(rows_of(out).columns) == ["x", "z"]


def test_a_brought_column_lands_under_its_authored_name():
    # `{z: z2}` lands the reference's z as z2 — an authored rename, never a silent suffix.
    stage = parse_stage({
        "id": "m", "description": "Join", "type": "enrich",
        "inputs": [{"id": "subject", "schema": _SUBJECT},
                   {"id": "reference", "schema": _REFERENCE}],
        "signature": {
            "form": "extends",
            "reads": [
                {"input": "subject", "columns": _SUBJECT["columns"]},
                {"input": "reference", "columns": _SUBJECT["columns"]},
            ],
            "adds": [{"name": "z2", "type": "str", "nullable": True}],
        },
        "join": {"keys": [{"left": "x", "right": "x"}], "enrich_with": {"z": "z2"}},
    })
    out = handle_enrich(
        stage,
        as_inputs({"subject": pd.DataFrame({"x": [1, 2]}),
         "reference": pd.DataFrame({"x": [1], "z": ["a"]})}),
        make_run_context(),
    )
    assert list(rows_of(out).columns) == ["x", "z2"]
    assert rows_of(out)["z2"].tolist()[0] == "a" and pd.isna(rows_of(out)["z2"].tolist()[1])


def test_a_right_key_sharing_a_subject_columns_name_is_dropped():
    # The reference's own `x` collides, but is narrowed away un-brought.
    stage = parse_stage({
        "id": "m", "description": "Join", "type": "enrich",
        "inputs": [{"id": "subject", "schema": _SUBJECT},
                   {"id": "reference",
                    "schema": {"columns": [{"name": "k", "type": "int", "nullable": True},
                                           {"name": "z", "type": "str", "nullable": True}]}}],
        "signature": {
            "form": "extends",
            "reads": [
                {"input": "subject", "columns": _SUBJECT["columns"]},
                {
                    "input": "reference",
                    "columns": [{"name": "k", "type": "int", "nullable": True}],
                },
            ],
            "adds": [{"name": "z", "type": "str", "nullable": True}],
        },
        "join": {"keys": [{"left": "x", "right": "k"}], "enrich_with": {"z": "z"}},
    })
    out = handle_enrich(
        stage,
        as_inputs({"subject": pd.DataFrame({"x": [1, 2]}),
         "reference": pd.DataFrame({"k": [1], "z": ["a"]})}),
        make_run_context(),
    )
    assert list(rows_of(out).columns) == ["x", "z"]
    assert rows_of(out)["z"].tolist()[0] == "a" and pd.isna(rows_of(out)["z"].tolist()[1])
