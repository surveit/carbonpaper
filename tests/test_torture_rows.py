"""Schema-driven torture-row fuzzing (app.runtime.torture_rows): the closed-loop
generation gate that EXECUTES each generated python stage against adversarial,
schema-derived edge rows and reports the ones that throw.

The load-bearing claim (issue #167): the four argcritic runtime bugs are invisible
to every static check because they depend on how pandas PHYSICALLY hands a cell to
the function — a `list[str]` cell as a numpy ndarray, a nullable cell as float NaN,
an empty upstream as a column-less frame — not on the (correct) logical schema. So
each is reproduced here on deliberately broken code and shown to pass once fixed.
"""
from __future__ import annotations

from app.core.models import Stage
from app.core.models.workflow import Workflow
from app.runtime.torture_rows import (
    find_torture_failures,
    run_stage_torture,
    synthesize_torture_cases,
    torture_gate,
)

# An argcritic-shaped input: an id, two nullable strings, and a list[str] column —
# exactly the columns whose runtime representations broke the four stages.
_ROW_IN = {"columns": [
    {"name": "doc_id", "type": "str", "nullable": False},
    {"name": "content", "type": "str", "nullable": True},
    {"name": "quote", "type": "str", "nullable": True},
    {"name": "tags", "type": "list[str]", "nullable": False},
]}
_ROW_OUT = {"columns": [
    {"name": "doc_id", "type": "str", "nullable": False},
    {"name": "ok", "type": "bool", "nullable": True},
]}


def _row_stage(code: str, *, stage_id: str = "s") -> Stage:
    return Stage.model_validate({
        "id": stage_id, "name": "S", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _ROW_IN}], "output_schema": _ROW_OUT,
        "function": {"kind": "inline", "code": code},
    })


_FRAME_IN = {"columns": [
    {"name": "claim_id", "type": "str", "nullable": False},
    {"name": "precision_passed", "type": "bool", "nullable": False},
]}
_FRAME_OUT = {"columns": [{"name": "claim_id", "type": "str", "nullable": False}]}


def _frame_stage(code: str, *, stage_id: str = "f") -> Stage:
    return Stage.model_validate({
        "id": stage_id, "name": "F", "type": "python_frame_function",
        "inputs": [{"id": "checks", "schema": _FRAME_IN}], "output_schema": _FRAME_OUT,
        "function": {"kind": "inline", "code": code},
    })


def _cases(failures) -> set[str]:
    return {failure.case for failure in failures}


# ── Bug 1: `if cell:` on a list[str] cell (ndarray) — truth value ambiguous ───
def test_if_cell_on_list_column_is_reproduced() -> None:
    # assemble_inference_contexts: `if cell:` on a Python list is fine; on the
    # ndarray the runtime hands, a multi- or zero-element cell raises ValueError.
    code = (
        "def transform(row):\n"
        "    flag = True if row['tags'] else False\n"
        "    return {'doc_id': row['doc_id'], 'ok': flag}\n"
    )
    failures = run_stage_torture(_row_stage(code))
    assert {"list_empty:load.tags", "list_multi:load.tags"} <= _cases(failures)
    assert all("truth value" in f.error for f in failures)


# ── Bug 2: `cell or []` on a list[str] cell (ndarray) — same trap ─────────────
def test_cell_or_default_on_list_column_is_reproduced() -> None:
    code = (
        "def transform(row):\n"
        "    tags = row['tags'] or []\n"
        "    return {'doc_id': row['doc_id'], 'ok': len(tags) > 0}\n"
    )
    failures = run_stage_torture(_row_stage(code))
    assert {"list_empty:load.tags", "list_multi:load.tags"} <= _cases(failures)


# ── Bug 3: `quote in content` when a nullable str arrives as float NaN ────────
def test_membership_on_nullable_str_that_is_nan_is_reproduced() -> None:
    # apply_groundedness_gate: `quote in content` assumes both are str; a missing
    # nullable cell arrives as float('nan'), and `x in nan` / `nan in x` raise TypeError.
    code = (
        "def transform(row):\n"
        "    hit = row['quote'] in row['content']\n"
        "    return {'doc_id': row['doc_id'], 'ok': hit}\n"
    )
    failures = run_stage_torture(_row_stage(code))
    assert {"null:load.content", "null:load.quote"} <= _cases(failures)
    assert all("TypeError" in f.error for f in failures)


# ── Bug 4: `df['col']` on an empty (column-less) frame — KeyError ─────────────
def test_column_select_on_empty_frame_is_reproduced() -> None:
    # surface_load_bearing_findings guard: an empty upstream output arrives as a
    # bare, column-less frame, so df['precision_passed'] raises KeyError.
    code = (
        "def transform(df):\n"
        "    return df[df['precision_passed']][['claim_id']]\n"
    )
    failures = run_stage_torture(_frame_stage(code))
    assert "empty_input_frame" in _cases(failures)
    assert any("KeyError" in f.error for f in failures)


# ── Fixed code survives every torture row ─────────────────────────────────────
def test_representation_robust_row_code_passes() -> None:
    code = (
        "def transform(row):\n"
        "    tags = list(row['tags']) if row['tags'] is not None else []\n"
        "    content, quote = row['content'], row['quote']\n"
        "    hit = (\n"
        "        bool(tags)\n"
        "        and isinstance(content, str)\n"
        "        and isinstance(quote, str)\n"
        "        and quote in content\n"
        "    )\n"
        "    return {'doc_id': row['doc_id'], 'ok': hit}\n"
    )
    assert run_stage_torture(_row_stage(code)) == []


def test_representation_robust_frame_code_passes() -> None:
    code = (
        "def transform(df):\n"
        "    if 'precision_passed' not in df.columns:\n"
        "        return df.reindex(columns=['claim_id'])\n"
        "    return df[df['precision_passed']][['claim_id']]\n"
    )
    assert run_stage_torture(_frame_stage(code)) == []


# ── Case synthesis is seeded entirely from the schema ─────────────────────────
_PASS_ROW = (
    "def transform(row):\n"
    "    return {'doc_id': row['doc_id'], 'ok': True}\n"
)


def test_cases_cover_empty_frame_nulls_and_both_list_shapes() -> None:
    names = [case.name for case in synthesize_torture_cases(_row_stage(_PASS_ROW))]
    assert "empty_input_frame" in names           # always
    assert "null:load.content" in names           # every nullable column
    assert "null:load.quote" in names
    assert "null:load.doc_id" not in names        # doc_id is non-nullable
    assert "list_empty:load.tags" in names        # every list column, empty …
    assert "list_multi:load.tags" in names        # … and multi-element


# ── The gate: find_torture_failures / torture_gate over a whole workflow ──────
def _input_stage() -> dict:
    return {
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file"},
        "output_schema": _ROW_IN,
    }


def _workflow(stage_code: str) -> Workflow:
    return Workflow.model_validate({"stages": [_input_stage(), _row_stage(stage_code, stage_id="s")]})


def test_find_torture_failures_names_the_broken_stage_and_skips_non_python() -> None:
    broken = _workflow("def transform(row):\n    return {'doc_id': row['doc_id'], 'ok': bool(row['tags'])}\n")
    failures = find_torture_failures(list(broken.stages))
    # Only the python stage contributes; the input_data source is skipped.
    assert {f.stage_id for f in failures} == {"s"}


def test_torture_gate_raises_with_actionable_message_on_a_broken_stage() -> None:
    broken = _workflow("def transform(row):\n    return {'doc_id': row['doc_id'], 'ok': bool(row['tags'])}\n")
    try:
        torture_gate(broken)
    except ValueError as err:
        message = str(err)
    else:
        raise AssertionError("torture_gate did not reject the broken workflow")
    assert "stage `s`" in message
    assert "torture row" in message
    assert "submit_answer" in message  # tells the agent what to do next


def test_torture_gate_is_silent_on_a_representation_robust_workflow() -> None:
    clean = _workflow(
        "def transform(row):\n"
        "    tags = list(row['tags']) if row['tags'] is not None else []\n"
        "    return {'doc_id': row['doc_id'], 'ok': len(tags) > 0}\n"
    )
    assert torture_gate(clean) is None  # nothing raised, nothing to fix


# ── Multi-input frame stage: torturing one input holds the others at baseline ─
def test_multi_input_frame_stage_tortures_each_input() -> None:
    left = {"columns": [
        {"name": "k", "type": "str", "nullable": False},
        {"name": "notes", "type": "str", "nullable": True},
    ]}
    right = {"columns": [
        {"name": "k", "type": "str", "nullable": False},
        {"name": "labels", "type": "list[str]", "nullable": False},
    ]}
    stage = Stage.model_validate({
        "id": "m", "name": "M", "type": "python_frame_function",
        "inputs": [{"id": "a", "schema": left}, {"id": "b", "schema": right}],
        "output_schema": {"columns": [{"name": "k", "type": "str", "nullable": False}]},
        # Robust: never assumes columns exist and coerces the list cell.
        "function": {"kind": "inline", "code": (
            "def transform(a, b):\n"
            "    return a.reindex(columns=['k'])\n"
        )},
    })
    names = [case.name for case in synthesize_torture_cases(stage)]
    assert "null:a.notes" in names            # left input's nullable column
    assert "list_empty:b.labels" in names     # right input's list column
    assert "list_multi:b.labels" in names
    assert run_stage_torture(stage) == []     # the robust code survives them all
