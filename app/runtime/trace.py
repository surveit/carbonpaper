"""Provenance tracer: walk a claim's row back through one run, by row ordinal
for a row-preserving stage or via its lineage sidecar otherwise (filter_rows,
union). Stops at any stage neither covers. Self-contained on the run
directory (manifest.json + outputs/<stage>[.lineage].parquet) - never reads
the compiled DAG, so later methodology edits don't affect it.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.errors import RowOutOfRange, StageNotInRun
from app.core.frames import PARQUET_SUFFIX
from app.models.stage import PositionalCross, StageType, find_positional_cross
from app.runtime.lineage import (
    TRACE_SOURCE_ROW_KEY,
    TRACE_SOURCE_STAGE_KEY,
    lineage_sidecar_path,
)


def _find_positional_cross(stage_type: str) -> PositionalCross | None:
    """Which input edge this stage's rows came from by position, or None for none.

    Delegates to the model's single classification rather than a tracer-local
    list — a newly crossable type, or a reclassified one, is picked up here
    automatically. This reads a static type taxonomy, not the run's compiled
    methodology, so the tracer stays self-contained on the run directory. A
    manifest type that isn't a known StageType (a foreign or future run) is
    never trusted. The fact is necessary but not sufficient: crossing is still
    gated on the stage recording the expected number of input edges (so an
    inconsistent manifest can't send the walk down the wrong branch) and on
    matching parent/child row counts (the len(parent_df) != len(df) guard below).
    """
    try:
        return find_positional_cross(StageType(stage_type))
    except ValueError:
        return None


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


def _load_manifest(run_dir: Path) -> dict[str, Any]:
    path = Path(run_dir) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"no manifest.json in {run_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def _stages_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["stage_id"]: s for s in manifest.get("stage_records", [])}


def _parents(stage_record: dict[str, Any]) -> list[str]:
    parents: list[str] = []
    for entry in stage_record.get("input_validation_report") or []:
        phase = entry.get("phase", "")
        if phase.startswith("input:"):
            parents.append(phase.split(":", 1)[1])
    return parents


def _origin(stage_type: str) -> str:
    return {
        "input_data": "source",
        "python_row_function": "computed",
        "llm_transform": "llm",
    }.get(stage_type, "other")


def _read_output(run_dir: Path, stage_record: dict[str, Any]) -> pd.DataFrame | None:
    rel = stage_record.get("output_path")
    if not rel:
        return None
    path = Path(run_dir) / rel
    if not path.exists():
        return None
    return pd.read_parquet(path) if path.suffix == PARQUET_SUFFIX else pd.read_csv(path)


def _scalar(value: Any) -> Any:
    # Parquet list/array cells (and numpy scalar types, e.g. float64) arrive
    # with .tolist(); convert to plain Python so the row is JSON-able.
    if hasattr(value, "tolist") and not isinstance(value, str):
        value = value.tolist()
    # A pandas-null numeric cell (NaN, +-inf) becomes None: JSON has no
    # non-finite float token, and 0 would misrepresent a value the source
    # never reported.
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _row_dict(df: pd.DataFrame, r: int) -> dict[str, Any]:
    return {str(k): _scalar(v) for k, v in df.iloc[r].items()}


def _lineage_hop(run_dir: Path, stage_id: str, row_ordinal: int) -> tuple[str, int] | None:
    """This stage's recorded parent (stage id, row ordinal) for `row_ordinal`,
    read from its lineage sidecar — present only for a stage type that records
    explicit per-row provenance (filter_rows, union; see
    app.runtime.lineage). None where no sidecar exists for this stage,
    or `row_ordinal` is out of its range."""
    path = lineage_sidecar_path(run_dir, stage_id)
    if not path.exists():
        return None
    lineage = pd.read_parquet(path)
    if row_ordinal < 0 or row_ordinal >= len(lineage):
        return None
    row = lineage.iloc[row_ordinal]
    return str(row[TRACE_SOURCE_STAGE_KEY]), int(row[TRACE_SOURCE_ROW_KEY])


def _new_columns(child: pd.DataFrame, parent: pd.DataFrame | None) -> list[str]:
    if parent is None:
        return [str(c) for c in child.columns]
    parent_cols = {str(c) for c in parent.columns}
    return [str(c) for c in child.columns if str(c) not in parent_cols]


def _not_preserving_message(stage_type: str) -> str:
    """The stop message when a stage can't be crossed. Names the type and the issue."""
    return (f"stops at {stage_type} — it reshapes rows, so row-level lineage "
            "across it needs recording (issue #58)")


def _subject_parent_id(stage_type: str, parents: list[str]) -> str | None:
    """The input edge this row positionally came from, or None if none pins one down."""
    cross = _find_positional_cross(stage_type)
    if cross is None or len(parents) != cross.input_count:
        return None
    return parents[cross.subject_input]


def _columns_parent_id(
    stage_type: str, parents: list[str], lineage_hop: tuple[str, int] | None
) -> str | None:
    """The id of the row's ACTUAL parent for `columns_new` purposes: the
    lineage-recorded one when there is one, else the subject input edge this
    type's rows positionally come from — None when neither pins down a single
    parent. For a join that means the SUBJECT input, so the columns the join
    brought over from its reference read as new at the join, which is where
    they entered this row."""
    if lineage_hop is not None:
        return lineage_hop[0]
    subject = _subject_parent_id(stage_type, parents)
    if subject is not None:
        return subject
    return parents[0] if len(parents) == 1 else None


def _advance_via_lineage(
    run_dir: Path, by_id: dict[str, dict[str, Any]], sid: str,
    parents: list[str], lineage_hop: tuple[str, int],
) -> tuple[str, int] | TraceEnd:
    """Cross via recorded per-row provenance — valid even when the stage isn't
    row-preserving BY POSITION (filter_rows, union)."""
    parent_id, parent_row = lineage_hop
    if parent_id not in parents:
        return TraceEnd(False, sid, "lineage names a parent not among this stage's input edges")
    if parent_id not in by_id:
        return TraceEnd(False, sid, "the parent named in lineage is not in the run")
    parent_output = _read_output(run_dir, by_id[parent_id])
    if parent_output is None:
        return TraceEnd(False, parent_id, "this stage's output file is missing from the run")
    if parent_row < 0 or parent_row >= len(parent_output):
        return TraceEnd(False, sid, "lineage names an out-of-range parent row")
    return parent_id, parent_row


def _advance_positionally(
    run_dir: Path, by_id: dict[str, dict[str, Any]], sid: str,
    parent_id: str, r: int, df: pd.DataFrame,
) -> tuple[str, int] | TraceEnd:
    """Cross a row-preserving stage's single input edge, keeping the same
    ordinal — the whole point of row-and-order preservation."""
    if parent_id not in by_id:
        return TraceEnd(False, sid, "the parent named in the manifest is not in the run")
    parent_df = _read_output(run_dir, by_id[parent_id])
    if parent_df is None:
        return TraceEnd(False, parent_id, "this stage's output file is missing from the run")
    if len(parent_df) != len(df):
        return TraceEnd(False, sid,
                         "this stage's row count differs from its input, so per-row "
                         "position can't be trusted (issue #58)")
    return parent_id, r


def _advance(
    run_dir: Path, by_id: dict[str, dict[str, Any]], sid: str, stage_type: str, r: int,
    df: pd.DataFrame, parents: list[str], lineage_hop: tuple[str, int] | None,
) -> tuple[str, int] | TraceEnd:
    """The next `(stage_id, row_ordinal)` to visit from `(sid, r)`, or the
    `TraceEnd` the walk stops on here: an `input_data` origin, no recorded
    input edge, a recorded lineage hop, or an ordinal cross into the input this
    type's rows positionally come from. A stage recording a different number of
    input edges than its type has is not crossed — the manifest disagrees with
    the type, so position can't be trusted."""
    if stage_type == StageType.input_data:
        return TraceEnd(True, sid, "input_data stage — the rows originate here")
    if not parents:
        return TraceEnd(False, sid, "the manifest records no input edge for this stage")
    if lineage_hop is not None:
        return _advance_via_lineage(run_dir, by_id, sid, parents, lineage_hop)
    subject = _subject_parent_id(stage_type, parents)
    if subject is None:
        return TraceEnd(False, sid, _not_preserving_message(stage_type))
    return _advance_positionally(run_dir, by_id, sid, subject, r, df)


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
        stage_type = record.get("type", "")
        df = _read_output(run_dir, record)
        if df is None:
            end = TraceEnd(False, sid, "this stage's output file is missing from the run")
            break
        if r < 0 or r >= len(df):
            raise RowOutOfRange(f"row {r} out of range for stage {sid!r} ({len(df)} rows)")

        parents = _parents(record)
        lineage_hop = _lineage_hop(run_dir, sid, r)
        columns_parent_id = _columns_parent_id(stage_type, parents, lineage_hop)
        parent_df = (
            _read_output(run_dir, by_id[columns_parent_id])
            if columns_parent_id in by_id else None
        )

        steps.append(StageTransform(
            stage_id=sid,
            stage_type=stage_type,
            row_ordinal=r,
            row=_row_dict(df, r),
            columns_new=_new_columns(df, parent_df),
            origin=_origin(stage_type),
        ))

        next_hop = _advance(run_dir, by_id, sid, stage_type, r, df, parents, lineage_hop)
        if isinstance(next_hop, TraceEnd):
            end = next_hop
        else:
            sid, r = next_hop

    return Trace(
        run_id=manifest.get("run_id", run_dir.name),
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
