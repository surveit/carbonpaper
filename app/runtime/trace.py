"""Positional provenance tracer: walk a claim's row back through the
row-preserving stages of one run, by row ordinal alone.

A stage is *row-preserving* when output row i is produced from input row i by
position. For such a chain the row ordinal is the cross-stage key, so nothing
needs to be recorded: the tracer just reads row i at each stage. At any stage
that reshapes rows the walk stops — the ancestry beyond it isn't positionally
recoverable (recorded lineage is issue #58).

Self-contained on the run directory: reads the run's manifest (stage type,
parent edges, row counts) from the document store, and
outputs/<stage>.parquet (row values) off disk. It never reads the compiled
DAG, so it is unaffected by later edits to the methodology.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.errors import DocumentNotFound, RowOutOfRange, StageNotInRun
from app.core.models.records.workflow_run import StageRun, WorkflowRun
from app.core.models.stage import StageType, is_grain_and_order_preserving


def _is_row_preserving(stage_type: str) -> bool:
    """True when this stage type guarantees output row i came from input row i by
    position, so the walk can cross the hop on ordinal alone.

    Delegates to the model's single classification
    (is_grain_and_order_preserving) rather than a tracer-local list — a newly
    preserving type, or a reclassified one, is picked up here automatically. This
    reads a static type taxonomy, not the run's compiled methodology, so the
    tracer stays self-contained on the run directory. A manifest type that isn't
    a known StageType (a foreign or future run) is never trusted. Membership is
    necessary but not sufficient: crossing is still gated on matching parent/child
    row counts too (the len(parent_df) != len(df) guard below). The runtime guarantees
    preservation by driving row-mapped stage types per-row (see app/runtime/stages/execution.py).
    """
    try:
        return is_grain_and_order_preserving(StageType(stage_type))
    except ValueError:
        return False


@dataclass
class StageTransform:
    """One stage on the traced path: the single row this stage contributed and
    how it entered. (One step of the walk — see `Trace.steps`.)"""
    stage_id: str
    stage_type: str
    row_ordinal: int
    row: dict[str, Any]     # the row's cells, verbatim
    columns_new: list[str]  # columns first appearing at this stage vs its parent
    origin: str             # "source" | "computed" | "llm" | "other"


@dataclass
class TraceEnd:
    """Where and why the walk stopped. `reached_origin` is True only when it
    landed on an `input_data` stage (a clean, complete trace); otherwise the
    walk could not cross a stage and `message` says why."""
    reached_origin: bool
    at_stage: str
    message: str


@dataclass
class Trace:
    run_id: str
    start_stage: str
    start_row: int
    steps: list[StageTransform]  # newest first: start stage, then each ancestor
    end: TraceEnd


def _load_manifest(run_dir: Path) -> WorkflowRun:
    """The run's WorkflowRun record, from the document store. `project`/
    `run_id` are derived from `run_dir`'s layout (`<project_dir>/runs/<run_id>`
    — the same convention `app.web.loading.load_manifest` derives from)."""
    run_dir = Path(run_dir)
    project = run_dir.parent.parent.name
    try:
        return WorkflowRun.load(f"{project}/{run_dir.name}")
    except DocumentNotFound as exc:
        raise FileNotFoundError(f"no run manifest for {run_dir}") from exc


def _stages_by_id(manifest: WorkflowRun) -> dict[str, StageRun]:
    return {s.stage_id: s for s in manifest.stages}


def _parents(stage_record: StageRun) -> list[str]:
    parents: list[str] = []
    for entry in stage_record.input_validation:
        if entry.phase.startswith("input:"):
            parents.append(entry.phase.split(":", 1)[1])
    return parents


def _origin(stage_type: str) -> str:
    return {
        "input_data": "source",
        "python_row_function": "computed",
        "llm_transform": "llm",
    }.get(stage_type, "other")


def _read_output(run_dir: Path, stage_record: StageRun) -> pd.DataFrame | None:
    rel = stage_record.output_path
    if not rel:
        return None
    path = Path(run_dir) / rel
    if not path.exists():
        return None
    return pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)


def _scalar(value: Any) -> Any:
    # Parquet list/array cells arrive as numpy arrays; make them plain lists so
    # the row is JSON-able. Leave everything else (including strings) untouched.
    if hasattr(value, "tolist") and not isinstance(value, str):
        return value.tolist()
    return value


def _row_dict(df: pd.DataFrame, r: int) -> dict[str, Any]:
    return {str(k): _scalar(v) for k, v in df.iloc[r].items()}


def _new_columns(child: pd.DataFrame, parent: pd.DataFrame | None) -> list[str]:
    if parent is None:
        return [str(c) for c in child.columns]
    parent_cols = {str(c) for c in parent.columns}
    return [str(c) for c in child.columns if str(c) not in parent_cols]


def _not_preserving_message(stage_type: str) -> str:
    """The stop message when a stage can't be crossed: the stage isn't row-
    preserving, so the ancestry isn't positionally recoverable. Names the stage
    type and points at the tracking issue. Also reached defensively for a
    row-preserving type carrying the wrong parent arity (len(parents) != 1)."""
    return (f"stops at {stage_type} — it reshapes rows, so row-level lineage "
            "across it needs recording (issue #58)")


def trace_row(run_dir: Path, stage_id: str, row_ordinal: int) -> Trace:
    """Trace one row's ancestry backward through row-preserving stages.

    Returns a `Trace` whose `steps` run newest-first from `(stage_id,
    row_ordinal)` to either an `input_data` origin or the first stage that
    cannot be crossed. Raises `StageNotInRun` / `RowOutOfRange` for a bad
    stage id or row ordinal (caller/param errors), never for a traceable state.
    """
    run_dir = Path(run_dir)
    manifest = _load_manifest(run_dir)
    by_id = _stages_by_id(manifest)
    if stage_id not in by_id:
        raise StageNotInRun(f"stage {stage_id!r} not in run {run_dir.name}")

    steps: list[StageTransform] = []
    sid, r = stage_id, row_ordinal
    end: TraceEnd | None = None

    while end is None:
        record = by_id[sid]
        stage_type = record.type
        df = _read_output(run_dir, record)
        if df is None:
            end = TraceEnd(False, sid, "this stage's output file is missing from the run")
            break
        if r < 0 or r >= len(df):
            raise RowOutOfRange(f"row {r} out of range for stage {sid!r} ({len(df)} rows)")

        parents = _parents(record)
        parent_df = None
        if len(parents) == 1 and parents[0] in by_id:
            parent_df = _read_output(run_dir, by_id[parents[0]])

        steps.append(StageTransform(
            stage_id=sid,
            stage_type=stage_type,
            row_ordinal=r,
            row=_row_dict(df, r),
            columns_new=_new_columns(df, parent_df),
            origin=_origin(stage_type),
        ))

        # Can we cross into the parent, keeping the same ordinal?
        if stage_type == "input_data":
            end = TraceEnd(True, sid, "input_data stage — the rows originate here")
        elif not parents:
            end = TraceEnd(False, sid, "the manifest records no input edge for this stage")
        # A row-preserving stage has exactly one input; more (or the wrong type)
        # means we can't trust position — treat it as not row-preserving.
        elif not _is_row_preserving(stage_type) or len(parents) != 1:
            end = TraceEnd(False, sid, _not_preserving_message(stage_type))
        else:
            parent_id = parents[0]
            if parent_id not in by_id:
                end = TraceEnd(False, sid, "the parent named in the manifest is not in the run")
            elif parent_df is None:
                end = TraceEnd(False, parent_id, "this stage's output file is missing from the run")
            elif len(parent_df) != len(df):
                end = TraceEnd(False, sid,
                               "this stage's row count differs from its input, so per-row "
                               "position can't be trusted (issue #58)")
            else:
                sid, r = parent_id, r  # same ordinal — the whole point

    return Trace(
        run_id=manifest.run_id,
        start_stage=stage_id,
        start_row=row_ordinal,
        steps=steps,
        end=end,
    )


def trace_to_dict(trace: Trace) -> dict[str, Any]:
    """Flatten a Trace to a JSON-able nested dict for the API and templates."""
    return {
        "run_id": trace.run_id,
        "start_stage": trace.start_stage,
        "start_row": trace.start_row,
        "steps": [
            {
                "stage_id": step.stage_id,
                "stage_type": step.stage_type,
                "row_ordinal": step.row_ordinal,
                "row": step.row,
                "columns_new": step.columns_new,
                "origin": step.origin,
            }
            for step in trace.steps
        ],
        "end": {
            "reached_origin": trace.end.reached_origin,
            "at_stage": trace.end.at_stage,
            "message": trace.end.message,
        },
    }
