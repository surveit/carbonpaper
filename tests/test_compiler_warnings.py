"""Compiler warnings: what is wrong with a workflow AS WRITTEN, with nothing run.

The gate the authoring agent must clear, and the list the Workflow page shows.
"""
from __future__ import annotations

from app import models as m
from app.models import find_stage_compiler_warnings, find_workflow_compiler_warnings

_SCHEMA = {"columns": [{"name": "id", "type": "str"}], "primary_key": ["id"]}
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
        "id": stage_id, "name": stage_id.replace("_", " ").title(), "type": type_,
        "inputs": [{"id": "up", "schema": _SCHEMA}],
        "output_schema": _SCHEMA,
        handle: block,
        **kw,
    }
    return m.Stage.model_validate(spec)


def _kinds(stage):
    return [w.kind for w in find_stage_compiler_warnings(stage)]


# ── a stage with nothing wrong ───────────────────────────────────────────────
def test_a_described_and_exemplified_stage_warns_about_nothing():
    assert _kinds(_stage(tests=[_PASSING_EXAMPLE])) == []


def test_a_config_only_stage_warns_about_nothing():
    """An enrich's keys are config a reviewer reads directly — no description to miss."""
    enrich = m.Stage.model_validate({
        "id": "j", "name": "J", "type": "enrich",
        "inputs": [{"id": "a", "schema": _SCHEMA}, {"id": "b", "schema": _SCHEMA}],
        "output_schema": _SCHEMA,
        "join": {"keys": [{"left": "id", "right": "id"}]},
    })
    assert _kinds(enrich) == []


# ── the blocking kinds ───────────────────────────────────────────────────────
def test_no_description_is_blocking():
    assert _kinds(_stage(summary=None)) == ["undescribed"]
    assert find_stage_compiler_warnings(_stage(summary=None))[0].blocking


def test_a_description_with_no_examples_is_blocking():
    """Nothing checks the description against the code, so it is unverified prose."""
    warnings = find_stage_compiler_warnings(_stage())
    assert [w.kind for w in warnings] == ["unexemplified"]
    assert warnings[0].blocking


def test_missing_description_outranks_missing_examples():
    """One warning per stage on this axis — fix the description first, then the
    examples that check it. Reporting both would be noise."""
    assert _kinds(_stage(summary=None)) == ["undescribed"]


def test_module_code_is_blocking_because_the_panel_cannot_show_it():
    warnings = find_stage_compiler_warnings(
        _stage(kind="module", module="pkg.mod", tests=[_PASSING_EXAMPLE]))
    assert [w.kind for w in warnings] == ["unreviewable_code"]
    assert warnings[0].blocking


# ── the non-blocking kinds ───────────────────────────────────────────────────
def test_an_untestable_type_is_not_blocking():
    """A filter_rows can never carry examples, so blocking on it would leave the
    agent no way to finish — but a reviewer still needs telling."""
    warnings = find_stage_compiler_warnings(
        _stage(stage_id="filt", type_="filter_rows", handle="filter"))
    assert [w.kind for w in warnings] == ["untestable"]
    assert not warnings[0].blocking


def test_cache_off_and_a_row_limit_are_notes_not_blockers():
    warnings = find_stage_compiler_warnings(
        _stage(cache=False, limit=100, tests=[_PASSING_EXAMPLE]))
    assert sorted(w.kind for w in warnings) == ["nondeterministic", "row_limit"]
    assert not any(w.blocking for w in warnings)


# ── the workflow-level gate ──────────────────────────────────────────────────
def test_a_workflow_is_clean_when_nothing_blocking_remains():
    """`is_clean` is the agent's gate, so a note must not hold it shut."""
    report = find_workflow_compiler_warnings([
        _stage(stage_id="ok", tests=[_PASSING_EXAMPLE]),
        _stage(stage_id="filt", type_="filter_rows", handle="filter"),
    ])
    assert report.warnings and report.is_clean
    assert report.blocking == []


def test_a_workflow_with_one_undescribed_stage_is_not_clean():
    report = find_workflow_compiler_warnings([
        _stage(stage_id="ok", tests=[_PASSING_EXAMPLE]),
        _stage(stage_id="silent", summary=None),
    ])
    assert not report.is_clean
    assert [w.stage_id for w in report.blocking] == ["silent"]


def test_blocking_warnings_sort_before_notes():
    """The page reads top-down and takes its colour from the first entry."""
    report = find_workflow_compiler_warnings([
        _stage(stage_id="filt", type_="filter_rows", handle="filter"),
        _stage(stage_id="silent", summary=None),
    ])
    assert [w.kind for w in report.warnings] == ["undescribed", "untestable"]
