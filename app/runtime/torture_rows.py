"""Schema-driven fuzzing for generated python transforms — the closed-loop gate.

A python_row_function / python_frame_function is written against the SCHEMA, and
every static check (per-stage validators, the graph checks, config-conformance)
holds the code to that schema. But the schema says `list[str]` and `str,
nullable`; it does not say that at RUNTIME a `list[str]` cell arrives as a numpy
ndarray, a nullable cell that is missing arrives as `float('nan')`, and an empty
upstream output arrives as a column-less frame. Code that is logically correct
about the schema can still be wrong about these physical representations — a whole
class of bug ("representation blindness") no static check can see, because it
depends on how pandas physically hands the cell to the function, not on the
(correct) logical type.

The only way to surface such a bug is to RUN the transform on a row that carries
the awkward representation. This module synthesizes those adversarial rows —
"torture rows" — from the stage's declared input `TableSchema` alone, and executes
the stage against them through the SAME handler registry the real runner uses
(fidelity comes from sharing the execution path, not reimplementing it — the same
principle app.runtime.stage_tests states). A stage that throws on any torture row
is reported with the exception; a clean sweep means the generated code survives the
representations it will actually meet.

The synthesized rows are deliberately adversarial (a clean happy-path row would go
green and ship the bug). Seeded only from the schema:
  - every `nullable: true` column      → a row where that cell is `float('nan')`;
  - every `list[...]` column           → an empty-ndarray row AND a >1-element
                                         ndarray row (a one-element list is
                                         unambiguous — the truth-value trap needs
                                         zero or many);
  - always                             → one run against a bare, column-less empty
                                         input frame.
Non-tortured cells carry a well-formed baseline value, and a stage's OTHER inputs
(a multi-input frame function) carry a baseline single row, so a failure is the
tortured representation's fault, not a missing dependency.

This is cheap: `python_row_function` / `python_frame_function` are deterministic
and need no model and no file inputs, so a full torture sweep runs in milliseconds.
The comparison is only "did it raise?" — the torture rows make no claim about the
OUTPUT (that is what authored StageTests are for); they only assert the code does
not blow up on the representation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.core.models import Column, Stage, TableSchema
from app.core.models.stage import StageType
from app.core.models.stages.stage_tests import STAGE_TEST_TYPES
from app.core.models.workflow import Workflow
from app.runtime.stages import HANDLERS

_LIST_RE = re.compile(r"^list\[(.+)\]$")


@dataclass
class TortureCase:
    """One adversarial scenario: the input frames to run the stage against, plus
    why the scenario is adversarial (surfaced in the failure message so a reader —
    or the generation agent — knows which representation broke the code)."""
    name: str
    reason: str
    frames: dict[str, pd.DataFrame]


@dataclass
class TortureFailure:
    """A stage that threw on one torture row: `case` names the scenario, `reason`
    says what made it adversarial, `error` is the exception (`Type: message`)."""
    stage_id: str
    case: str
    reason: str
    error: str


@dataclass
class _StageTorture:
    """Internal accumulator while synthesizing a stage's cases."""
    cases: list[TortureCase] = field(default_factory=list)


def synthesize_torture_cases(stage: Stage) -> list[TortureCase]:
    """The torture rows for `stage`, derived from its declared input schemas only.

    Always includes the empty-input case (every input a bare, column-less frame).
    For each input that declares a schema, adds: one null-cell case per nullable
    column, and empty-list + multi-element cases per `list[...]` column. Inputs
    without a declared schema can only contribute the empty-input case."""
    baseline = {ref.id: _baseline_frame(ref.table_schema) for ref in stage.inputs}

    cases: list[TortureCase] = [
        TortureCase(
            name="empty_input_frame",
            reason="empty upstream output arrives as a bare, column-less frame",
            frames={ref.id: pd.DataFrame() for ref in stage.inputs},
        )
    ]

    for ref in stage.inputs:
        schema = ref.table_schema
        if schema is None:
            continue
        for column in schema.columns:
            cases.extend(_column_cases(ref.id, column, schema, baseline))
    return cases


def _column_cases(
    input_id: str,
    column: Column,
    schema: TableSchema,
    baseline: dict[str, pd.DataFrame],
) -> list[TortureCase]:
    """The torture cases a single column contributes: a NaN row if it is nullable,
    and empty/multi-element ndarray rows if it is a `list[...]` column. Each holds
    every other cell (and every other input) at its baseline, so the tortured cell
    is the only variable."""
    cases: list[TortureCase] = []
    inner = _list_inner_type(column.type)

    if column.nullable:
        cases.append(
            _one_row_case(
                input_id, schema, baseline,
                name=f"null:{input_id}.{column.name}",
                reason=(
                    f"nullable column {column.name!r} is null — arrives as "
                    "float('nan'), not None"
                ),
                override={column.name: float("nan")},
            )
        )

    if inner is not None:
        cases.append(
            _one_row_case(
                input_id, schema, baseline,
                name=f"list_empty:{input_id}.{column.name}",
                reason=(
                    f"list column {column.name!r} is empty — arrives as an empty "
                    "numpy ndarray, not []"
                ),
                override={column.name: np.array([], dtype=object)},
            )
        )
        cases.append(
            _one_row_case(
                input_id, schema, baseline,
                name=f"list_multi:{input_id}.{column.name}",
                reason=(
                    f"list column {column.name!r} has several elements — arrives as "
                    "a numpy ndarray, whose truth value is ambiguous"
                ),
                override={
                    column.name: np.array(
                        [_baseline_scalar(inner), _baseline_scalar(inner)],
                        dtype=object,
                    )
                },
            )
        )
    return cases


def _one_row_case(
    input_id: str,
    schema: TableSchema,
    baseline: dict[str, pd.DataFrame],
    *,
    name: str,
    reason: str,
    override: dict[str, Any],
) -> TortureCase:
    """A case where `input_id` carries a single row — every column at its baseline
    except those in `override` — and every other input carries its baseline frame."""
    row = {column.name: _baseline_cell(column) for column in schema.columns}
    row.update(override)
    frames = dict(baseline)
    frames[input_id] = _frame_from_row(schema, row)
    return TortureCase(name=name, reason=reason, frames=frames)


def run_stage_torture(stage: Stage) -> list[TortureFailure]:
    """Execute `stage` against each of its torture rows through the stage's
    registered handler; return one TortureFailure per row the stage throws on
    ([] = the stage survives every synthesized representation). Raises ValueError
    for stage types that carry no runnable python function."""
    if stage.type not in STAGE_TEST_TYPES:
        raise ValueError(
            f"stage {stage.id} ({stage.type}) is not a runnable python transform"
        )
    handler = HANDLERS[StageType(stage.type)]
    failures: list[TortureFailure] = []
    for case in synthesize_torture_cases(stage):
        try:
            handler.execute(stage, dict(case.frames), ctx={})
        except Exception as exc:  # noqa: BLE001 — the function is authored code; any raise IS the result
            failures.append(
                TortureFailure(
                    stage_id=stage.id,
                    case=case.name,
                    reason=case.reason,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return failures


def find_torture_failures(stages: list[Stage]) -> list[TortureFailure]:
    """Every torture failure across `stages`' python transforms. Stages of any
    other type (or a python transform with no function block) contribute nothing.
    The closed-loop gate: [] means no generated stage throws on its torture rows."""
    failures: list[TortureFailure] = []
    for stage in stages:
        if stage.type not in STAGE_TEST_TYPES or stage.function is None:
            continue
        failures.extend(run_stage_torture(stage))
    return failures


def torture_gate(workflow: Workflow) -> None:
    """The generation loop's closed-loop gate, shaped as an Agent `post_validate`
    hook: run every generated python stage against its torture rows and raise
    `ValueError` — carrying the per-stage tracebacks the agent must fix — if any
    stage throws. Silent when the workflow survives (no generated stage blows up on
    the representations it will actually meet). Passing this to build_workflow_agent
    is what turns generation from open-loop (submit → shape-check → write) into
    closed-loop (submit → shape-check → EXECUTE → repair-or-write)."""
    failures = find_torture_failures(list(workflow.stages))
    if failures:
        raise ValueError(format_torture_failures(failures))


def format_torture_failures(failures: list[TortureFailure]) -> str:
    """Render torture failures as the corrective message the generation agent
    reads: what representation broke each stage, and the exception it raised."""
    lines = [
        "Generated stage(s) threw when executed against schema-derived torture "
        "rows — edge representations that pass every static check but arise at "
        "RUNTIME: a null cell arrives as float('nan') (not None), a list[...] cell "
        "as a numpy ndarray (not a Python list, so `if cell:` / `cell or []` on a "
        "multi-element cell raises), and an empty upstream output as a column-less "
        "frame (so `df['col']` raises KeyError). Make each stage handle these, then "
        "call submit_answer again:"
    ]
    for failure in failures:
        lines.append(
            f"- stage `{failure.stage_id}` on torture row `{failure.case}` "
            f"({failure.reason}): {failure.error}"
        )
    return "\n".join(lines)


# ── Representation-faithful frame construction ───────────────────────────────
def _frame_from_row(schema: TableSchema, row: dict[str, Any]) -> pd.DataFrame:
    """A one-row frame whose cells keep the physical representation they are given
    — an ndarray stays an ndarray, a float('nan') stays a nan float — so the row a
    handler reads back (via `to_dict('records')`, exactly as the runner does)
    carries the adversarial representation, not a pandas-normalized copy."""
    columns = [column.name for column in schema.columns]
    frame = pd.DataFrame({name: pd.Series([row[name]], dtype=object) for name in columns})
    return frame[columns]


def _baseline_frame(schema: TableSchema | None) -> pd.DataFrame:
    """A single well-formed row for an input held at baseline. A schema-less input
    can only be an empty frame — its columns are unknown."""
    if schema is None:
        return pd.DataFrame()
    row = {column.name: _baseline_cell(column) for column in schema.columns}
    return _frame_from_row(schema, row)


def _baseline_cell(column: Column) -> Any:
    """A well-formed, non-null value for `column`. A list column's baseline is a
    ONE-element ndarray: it matches the runtime representation (ndarray, not list)
    without tripping the multi-element truth-value trap the list_multi case owns."""
    inner = _list_inner_type(column.type)
    if inner is not None:
        return np.array([_baseline_scalar(inner)], dtype=object)
    if column.type == "json" or column.type == "list[json]":
        obj = _baseline_json(column)
        if column.type == "list[json]":
            return np.array([obj], dtype=object)
        return obj
    return _baseline_scalar_column(column)


def _baseline_json(column: Column) -> dict[str, Any]:
    """A baseline object for a `json`/`list[json]` column: one baseline value per
    declared field, or a single entry for an open `value_type` map."""
    if column.fields is not None:
        return {field.name: _baseline_cell(field) for field in column.fields}
    assert column.value_type is not None  # Column._json_shape guarantees one of the two
    return {"key": _baseline_scalar(column.value_type)}


def _baseline_scalar_column(column: Column) -> Any:
    """Baseline for a scalar column, honoring an `enum` vocabulary when present."""
    if column.enum:
        return column.enum[0]
    return _baseline_scalar(column.type)


def _baseline_scalar(type_name: str) -> Any:
    """A representative non-null value for a scalar column type."""
    match type_name:
        case "str":
            return "x"
        case "int":
            return 1
        case "float":
            return 1.0
        case "bool":
            return True
        case "date":
            return "2020-01-01"
        case "datetime":
            return "2020-01-01T00:00:00"
        case _:
            # json/list[...] inner types fall back to a neutral object; the caller
            # only reaches here for scalar inners.
            return "x"


def _list_inner_type(type_name: str) -> str | None:
    """The inner type of a `list[...]` column (e.g. 'str' for 'list[str]'), or None
    when the column is not a list."""
    match = _LIST_RE.match(type_name)
    return match.group(1).strip() if match else None
