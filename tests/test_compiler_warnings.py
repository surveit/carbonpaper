"""Compiler warnings: what is wrong with a workflow AS WRITTEN, with nothing run.

The gate the authoring agent must clear, and the list the Workflow page shows.
"""
from __future__ import annotations

import pytest
from conftest import reads_of

from app import models as m
from app.models import find_stage_compiler_warnings, find_workflow_compiler_warnings

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

_CODE = "def transform(row):\n    return row"
_PASSING_EXAMPLE = {"name": "passes_through",
                    "inputs": {"up": [{"id": "r1"}]}, "expected": [{"id": "r1"}]}


def _stage(stage_id="s", type_="python_row_function", handle="function", **kw):
    block = {"summary": kw.pop("summary", "Passes every row through unchanged."),
             "code": _CODE if handle == "function" else "def should_include(row):\n    return True"}
    if handle == "function":
        block = {"kind": kw.pop("kind", "inline"), **block}
        if block["kind"] == "module":
            block = {**block, "module": kw.pop("module", "pkg.mod")}
            block.pop("code")
    spec = {
        "id": stage_id, "description": stage_id.replace("_", " ").title(), "type": type_,
        "inputs": [{"id": "up"}],
        "signature": _signature_for(type_, _SCHEMA),
        handle: block,
        **kw,
    }
    return m.parse_stage(spec)


# llm_transform caches by default; its author may turn caching off.
def _llm_stage(stage_id="ask", **kw):
    return m.parse_stage({
        "id": stage_id, "description": "Ask the model", "type": "llm_transform",
        "inputs": [{"id": "up"}],
        "signature": {
            "form": "extends",
            "reads": reads_of("up", _SCHEMA["columns"]),
            "adds": [{"name": "answer", "type": "str", "nullable": True}],
        },
        "llm": {"prompt_template": "classify {id}"},
        **kw,
    })


def _queue_stage(stage_id="rev", **kw):
    return m.parse_stage({
        "id": stage_id, "description": "A human checks each row", "type": "human_review_queue",
        "inputs": [{"id": "up"}],
        "signature": {
            "form": "extends",
            "reads": reads_of("up", _SCHEMA["columns"]),
            "adds": [
                {"name": "reviewed_id", "type": "str", "nullable": True},
                {"name": "verdict", "type": "str", "nullable": True},
                {"name": "reviewer", "type": "str", "nullable": True},
                {"name": "reviewed_at", "type": "str", "nullable": True},
            ],
        },
        "queue": {"reviewed_columns": {"id": "reviewed_id"}, "verdict_column": "verdict",
                  "reviewer_column": "reviewer", "reviewed_at_column": "reviewed_at"},
        **kw,
    })


def _kinds(stage):
    return [w.kind for w in find_stage_compiler_warnings(stage)]


# ── a stage with nothing wrong ───────────────────────────────────────────────
def test_a_described_and_exemplified_stage_warns_about_nothing():
    assert _kinds(_stage(tests=[_PASSING_EXAMPLE])) == []


def test_a_config_only_stage_warns_about_nothing():
    enrich = m.parse_stage({
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
    assert _kinds(enrich) == []


# ── the error kinds ───────────────────────────────────────────────────────
def test_no_description_is_a_warning():
    # An author may knowingly leave one standing; nothing here refuses an action.
    assert _kinds(_stage(summary=None)) == ["undescribed"]
    assert find_stage_compiler_warnings(_stage(summary=None))[0].severity == "warning"


def test_a_description_with_no_examples_warns():
    warnings = find_stage_compiler_warnings(_stage())
    assert [w.kind for w in warnings] == ["unexemplified"]
    assert warnings[0].severity == "warning"


def test_missing_description_outranks_missing_examples():
    assert _kinds(_stage(summary=None)) == ["undescribed"]


def _report_stage(stage_id="pub", **kw):
    return m.parse_stage({
        "id": stage_id, "description": "Pub", "type": "report",
        "signature": {"form": "replaces"},
        "inputs": [{"id": "up"}],
        "report": {"format": "csv"},
        "function": {"kind": "inline", "summary": "Writes one file per row.",
                     "code": "def transform(df, output_dir, citation_provider):\n    return df"},
        **kw,
    })


# ── the warning kinds ───────────────────────────────────────────────────
def test_a_type_that_cannot_run_examples_warns_about_nothing():
    assert _kinds(_report_stage()) == []


def test_a_type_that_cannot_run_examples_still_owes_a_description():
    stage = _report_stage()
    stage.function.summary = None
    assert _kinds(stage) == ["undescribed"]


def test_a_filter_with_no_examples_is_unexemplified():
    warnings = find_stage_compiler_warnings(
        _stage(stage_id="filt", type_="filter_rows", handle="filter"))
    assert [w.kind for w in warnings] == ["unexemplified"]


def test_an_llm_stage_with_cache_off_is_a_note_not_a_blocker():
    warnings = find_stage_compiler_warnings(_llm_stage(cache=False))
    assert [w.kind for w in warnings] == ["nondeterministic"]
    assert all(w.severity == "warning" for w in warnings)


def test_the_two_caching_types_warn_about_nothing_when_left_alone():
    assert _kinds(_llm_stage()) == []
    assert _kinds(_queue_stage()) == []


def test_a_code_stage_not_caching_is_the_default_and_says_nothing():
    # Cache off is not a choice here, so it is not one to tell a reviewer about.
    assert _kinds(_stage(tests=[_PASSING_EXAMPLE])) == []


# ── rows nobody has a word for ───────────────────────────────────────────────
_K = {"name": "k", "type": "str", "nullable": True}
_N = {"name": "n", "type": "int", "nullable": True}
_TOTAL = {"name": "total", "type": "int", "nullable": True}
_ITEMS = {"name": "items", "type": "list[str]", "nullable": True}
_ONE_ITEM = {"name": "items", "type": "str", "nullable": True}
_A_SUM = {"output_column": "total", "formula": "sum", "value_column": "n"}

# Each as small as its own model allows, so only the type varies between them.
_ROW_MINTING_SPECS = {
    "input_data": {
        "type": "input_data", "connector": {"kind": "file"},
        "signature": {"form": "replaces", "produces": [_K]},
    },
    "aggregate": {
        "type": "aggregate", "inputs": [{"id": "up"}],
        "signature": {"form": "replaces", "reads": reads_of("up", [_K, _N]),
                      "produces": [_K, _TOTAL]},
        "aggregate": {"group_by": ["k"], "aggregations": [_A_SUM]},
    },
    "dedupe": {
        "type": "dedupe", "inputs": [{"id": "up"}],
        "signature": {"form": "extends", "reads": reads_of("up", [_K])},
        "dedupe": {"keys": ["k"], "keep": "agree"},
    },
    "explode": {
        "type": "explode", "inputs": [{"id": "up"}],
        "signature": {"form": "extends", "reads": reads_of("up", [_ITEMS]),
                      "rewrites": [_ONE_ITEM]},
        "explode": {"column": "items"},
    },
    "expand": {
        "type": "expand", "inputs": [{"id": "up"}, {"id": "ref"}],
        "signature": {"form": "extends",
                      "reads": reads_of("up", [_K]) + reads_of("ref", [_K, _N]),
                      "adds": [_N]},
        "join": {"keys": [{"left": "k", "right": "k"}], "enrich_with": {"n": "n"}},
    },
    "python_frame_function": {
        "type": "python_frame_function", "inputs": [{"id": "up"}],
        "signature": {"form": "replaces", "reads": reads_of("up", [_K]), "produces": [_K]},
        "function": {"kind": "inline", "summary": "Pivots the frame.",
                     "code": "def transform(frame):\n    return frame",
                     "corner_cases": [{"case": "no rows", "expected": "no rows"}]},
    },
}


def _row_minting_stage(type_, **kw):
    return m.parse_stage({"id": f"s_{type_}", "description": f"Do {type_}",
                          **_ROW_MINTING_SPECS[type_], **kw})


@pytest.mark.parametrize("type_", sorted(_ROW_MINTING_SPECS))
def test_every_type_minting_a_new_kind_of_row_warns_when_it_names_none(type_):
    assert "unnamed_rows" in _kinds(_row_minting_stage(type_))


@pytest.mark.parametrize("type_", sorted(_ROW_MINTING_SPECS))
def test_naming_the_row_type_clears_the_warning(type_):
    assert "unnamed_rows" not in _kinds(_row_minting_stage(type_, row_type_id="facility"))


def test_the_warning_says_what_the_reader_loses_not_that_a_field_is_empty():
    [warning] = [w for w in find_stage_compiler_warnings(_row_minting_stage("dedupe"))
                 if w.kind == "unnamed_rows"]
    assert warning.severity == "warning"
    assert warning.detail == (
        "its rows are a new kind of thing and no `row_type_id` says what one of them "
        "is, so nothing written about them — this stage's own description, a review "
        "guide, a published figure — can name the thing"
    )


def test_a_stage_whose_rows_are_still_its_inputs_kind_of_thing_says_nothing():
    assert _kinds(_stage(tests=[_PASSING_EXAMPLE])) == []
    assert _kinds(_llm_stage()) == []


def _ungrouped_aggregate(**kw):
    return m.parse_stage({
        "id": "one_figure", "description": "Total everything", "type": "aggregate",
        "inputs": [{"id": "up"}],
        "signature": {"form": "replaces", "reads": reads_of("up", [_N]),
                      "produces": [_TOTAL]},
        "aggregate": {"group_by": [], "aggregations": [_A_SUM]},
        **kw,
    })


def test_the_two_stages_that_answer_for_themselves_are_owed_no_word():
    assert _kinds(_ungrouped_aggregate()) == []
    assert _kinds(_report_stage()) == []


def test_unnamed_rows_sorts_above_the_kinds_that_leave_words_unchecked():
    report = find_workflow_compiler_warnings([
        _llm_stage(stage_id="note", cache=False),
        _row_minting_stage("python_frame_function"),
    ])
    assert [w.kind for w in report.warnings] == [
        "unnamed_rows", "unexemplified", "nondeterministic"]


# ── the workflow-level gate ──────────────────────────────────────────────────
def test_every_compiler_note_is_a_warning():
    # None of them refuses an action, so none of them borrows the runtime's word for
    # a stage that stopped.
    report = find_workflow_compiler_warnings([
        _stage(stage_id="bare"),
        _stage(stage_id="silent", summary=None),
        _llm_stage(stage_id="note", cache=False),
    ])
    assert report.warnings
    assert all(w.severity == "warning" for w in report.warnings)


def test_a_workflow_with_one_undescribed_stage_still_warns_about_it():
    report = find_workflow_compiler_warnings([
        _stage(stage_id="ok", tests=[_PASSING_EXAMPLE]),
        _stage(stage_id="silent", summary=None),
    ])
    assert [w.stage_id for w in report.warnings] == ["silent"]


def test_the_least_reviewable_kinds_sort_first():
    report = find_workflow_compiler_warnings([
        _llm_stage(stage_id="note", cache=False),
        _stage(stage_id="bare"),
    ])
    assert [w.kind for w in report.warnings] == ["unexemplified", "nondeterministic"]


# ── examples that do not pass ────────────────────────────────────────────────
def test_failing_examples_are_a_warning_recommending_review():
    warnings = find_stage_compiler_warnings(_stage(tests=[_PASSING_EXAMPLE]), failing_examples=1)
    assert [w.kind for w in warnings] == ["examples_failing"]
    # Not an error: the agent may have read the description a different way, so a
    # human deciding the code is right resolves it with no edit to the stage.
    assert warnings[0].severity == "warning"
    assert warnings[0].detail == (
        "1 of its 1 examples mismatches what an independent AI agent expected. "
        "Further review recommended"
    )


def test_the_count_agrees_with_its_verb():
    [warning] = find_stage_compiler_warnings(
        _stage(tests=[_PASSING_EXAMPLE, {**_PASSING_EXAMPLE, "name": "also_passes"}]),
        failing_examples=2,
    )
    assert "2 of its 2 examples mismatch what" in warning.detail


def test_examples_are_judged_statically_when_the_caller_ran_nothing():
    assert _kinds(_stage(tests=[_PASSING_EXAMPLE])) == []


def test_missing_examples_outranks_failing_ones():
    assert _kinds(_stage(), ) == ["unexemplified"]


def test_a_workflow_with_a_failing_example_says_which_stage():
    report = find_workflow_compiler_warnings(
        [_stage(stage_id="ok", tests=[_PASSING_EXAMPLE]),
         _stage(stage_id="broken", tests=[_PASSING_EXAMPLE])],
        {"broken": 2},
    )
    assert [(w.stage_id, w.kind) for w in report.warnings] == [("broken", "examples_failing")]

