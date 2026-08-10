"""Compiler warnings: what is wrong with a workflow AS WRITTEN, with nothing run.

The gate the authoring agent must clear, and the list the Workflow page shows.
"""
from __future__ import annotations

from app import models as m
from app.models import find_stage_compiler_warnings, find_workflow_compiler_warnings

_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": True}]}
# Which signature form a type takes: the reshaping family replaces its input,
# the anchored family extends it.
_REPLACES_TYPES = {"pandas_frame_function", "aggregate", "union", "input_data", "publish"}


def _signature_for(type_, schema):
    if type_ == "publish":
        return {"form": "replaces"}
    if type_ in _REPLACES_TYPES:
        return {"form": "replaces", "produces": schema["columns"]}
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
        "inputs": [{"id": "up", "schema": _SCHEMA}],
        "signature": _signature_for(type_, _SCHEMA),
        handle: block,
        **kw,
    }
    return m.parse_stage(spec)


def _kinds(stage):
    return [w.kind for w in find_stage_compiler_warnings(stage)]


# ── a stage with nothing wrong ───────────────────────────────────────────────
def test_a_described_and_exemplified_stage_warns_about_nothing():
    assert _kinds(_stage(tests=[_PASSING_EXAMPLE])) == []


def test_a_config_only_stage_warns_about_nothing():
    """An enrich's keys are config a reviewer reads directly — no description to miss."""
    enrich = m.parse_stage({
        "id": "j", "description": "J", "type": "enrich",
        "inputs": [{"id": "a", "schema": _SCHEMA},
                   {"id": "b", "schema": {"columns": [{"name": "id", "type": "str", "nullable": True},
                                                      {"name": "v", "type": "str", "nullable": True}]}}],
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
def test_no_description_is_an_error():
    assert _kinds(_stage(summary=None)) == ["undescribed"]
    assert find_stage_compiler_warnings(_stage(summary=None))[0].severity == "error"


def test_a_description_with_no_examples_is_an_error():
    """Nothing checks the description against the code, so it is unverified prose."""
    warnings = find_stage_compiler_warnings(_stage())
    assert [w.kind for w in warnings] == ["unexemplified"]
    assert warnings[0].severity == "error"


def test_missing_description_outranks_missing_examples():
    """One warning per stage on this axis: fix the description first. Both at once is noise."""
    assert _kinds(_stage(summary=None)) == ["undescribed"]


def test_module_code_is_an_error_because_the_panel_cannot_show_it():
    warnings = find_stage_compiler_warnings(
        _stage(kind="module", module="pkg.mod", tests=[_PASSING_EXAMPLE]))
    assert [w.kind for w in warnings] == ["unreviewable_code"]
    assert warnings[0].severity == "error"


def _publish_stage(stage_id="pub"):
    """Authored code a reviewer must trust prose for, and no handler to run an example."""
    return m.parse_stage({
        "id": stage_id, "description": "Pub", "type": "publish",
        "signature": {"form": "replaces"},
        "inputs": [{"id": "up", "schema": _SCHEMA}],
        "publish": {"format": "csv"},
        "function": {"kind": "inline", "summary": "Writes one file per row.",
                     "code": "def transform(df, output_dir, trace_links):\n    return df"},
    })


# ── the warning kinds ───────────────────────────────────────────────────
def test_a_type_that_cannot_run_examples_warns_about_nothing():
    """A publish stage can never carry examples, so there is nothing to ask it for."""
    assert _kinds(_publish_stage()) == []


def test_a_type_that_cannot_run_examples_still_owes_a_description():
    """Dropping the example demand does not drop the prose demand."""
    stage = _publish_stage()
    stage.function.summary = None
    assert _kinds(stage) == ["undescribed"]


def test_a_filter_with_no_examples_is_unexemplified():
    """filter_rows CAN carry examples, so the honest complaint is that it has none."""
    warnings = find_stage_compiler_warnings(
        _stage(stage_id="filt", type_="filter_rows", handle="filter"))
    assert [w.kind for w in warnings] == ["unexemplified"]


def test_cache_off_and_a_row_limit_are_notes_not_blockers():
    warnings = find_stage_compiler_warnings(
        _stage(cache=False, limit=100, tests=[_PASSING_EXAMPLE]))
    assert sorted(w.kind for w in warnings) == ["nondeterministic", "row_limit"]
    assert all(w.severity == "warning" for w in warnings)


# ── the workflow-level gate ──────────────────────────────────────────────────
def test_a_workflow_is_clean_when_no_error_remains():
    """`is_clean` is the agent's gate, so a note must not hold it shut."""
    report = find_workflow_compiler_warnings([
        _stage(stage_id="ok", tests=[_PASSING_EXAMPLE]),
        _stage(stage_id="note", cache=False, tests=[_PASSING_EXAMPLE]),
    ])
    assert report.warnings and report.is_clean
    assert report.errors == []


def test_a_workflow_with_one_undescribed_stage_is_not_clean():
    report = find_workflow_compiler_warnings([
        _stage(stage_id="ok", tests=[_PASSING_EXAMPLE]),
        _stage(stage_id="silent", summary=None),
    ])
    assert not report.is_clean
    assert [w.stage_id for w in report.errors] == ["silent"]


def test_errors_sort_before_warnings():
    """The page reads top-down and takes its colour from the first entry."""
    report = find_workflow_compiler_warnings([
        _stage(stage_id="note", cache=False, tests=[_PASSING_EXAMPLE]),
        _stage(stage_id="silent", summary=None),
    ])
    assert [w.kind for w in report.warnings] == ["undescribed", "nondeterministic"]


# ── examples that do not pass ────────────────────────────────────────────────
def test_failing_examples_are_an_error():
    """Examples disagreeing with the code is not signed-off-able."""
    warnings = find_stage_compiler_warnings(_stage(tests=[_PASSING_EXAMPLE]), failing_examples=1)
    assert [w.kind for w in warnings] == ["examples_failing"]
    assert warnings[0].severity == "error"
    assert "1 of its 1 examples" in warnings[0].detail


def test_examples_are_judged_statically_when_the_caller_ran_nothing():
    """Omitting the count judges the stage as written — the pre-existing behaviour."""
    assert _kinds(_stage(tests=[_PASSING_EXAMPLE])) == []


def test_missing_examples_outranks_failing_ones():
    """A stage with no examples cannot also have failing ones; report the absence."""
    assert _kinds(_stage(), ) == ["unexemplified"]


def test_a_workflow_with_a_failing_example_is_not_clean():
    report = find_workflow_compiler_warnings(
        [_stage(stage_id="ok", tests=[_PASSING_EXAMPLE]),
         _stage(stage_id="broken", tests=[_PASSING_EXAMPLE])],
        {"broken": 2},
    )
    assert not report.is_clean
    assert [(w.stage_id, w.kind) for w in report.errors] == [("broken", "examples_failing")]

