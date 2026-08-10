"""Provenance tracer: walk a claim's row back through one run, by row ordinal
for a row-preserving stage or via its lineage sidecar otherwise (filter_rows,
union, join). Self-contained on the run directory - never reads the compiled
DAG, so later methodology edits don't affect it. Stays a single CHAIN even
where a row has several parents, stopping where it cannot cross (`_split_spine`)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.errors import RowOutOfRange, StageNotInRun
from app.core.frames import read_frame_file
from app.models.stage import StageType, is_grain_and_order_preserving
from app.runtime.lineage import (
    EdgeKind,
    RowLineage,
    RowParent,
    lineage_sidecar_path,
)
from app.runtime.manifest import read_run_manifest, resolve_output_path


def _is_row_preserving(stage_type: str) -> bool:
    """True when this type guarantees output row i came from input row i by position."""
    # Delegates to the model's single classification, so a reclassified type is
    # picked up here automatically; a type name this build doesn't know (a
    # foreign or future run) is never trusted. Necessary but NOT sufficient —
    # crossing is still gated on matching parent/child row counts below.
    try:
        return is_grain_and_order_preserving(StageType(stage_type))
    except ValueError:
        return False


@dataclass
class StageTransform:
    """One step of the walk: the single row this stage contributed, and how it entered."""
    stage_id: str
    stage_type: str
    row_ordinal: int
    row: dict[str, Any]     # the row's cells, verbatim
    columns_new: list[str]  # columns first appearing at this stage vs its parent
    origin: str             # "source" | "computed" | "llm" | "other"
    # Recorded parents of this row that the walk did NOT follow — the other side
    # of a join, and later an aggregate's contributors. Each is a starting point
    # for a trace of its own, which is how the reader promotes a branch onto the
    # spine; the walk itself stays a single chain (see `Trace.steps`).
    branches: list[RowParent] = field(default_factory=list)


@dataclass
class TraceEnd:
    """Where and why the walk stopped; `reached_origin` only on a clean landing at input_data."""
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
    """The run's recorded manifest, found by the (project, run id) its dir names."""
    return read_run_manifest(run_dir.parent.parent.name, run_dir.name).to_dict()


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
        "starlark_row_function": "computed",
        "llm_transform": "llm",
    }.get(stage_type, "other")


def _read_output(run_dir: Path, stage_record: dict[str, Any]) -> pd.DataFrame | None:
    path = resolve_output_path(Path(run_dir), stage_record.get("output_path"))
    if path is None or not path.exists():
        return None
    return read_frame_file(path)


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


def _lineage_hops(run_dir: Path, stage_id: str, row_ordinal: int) -> list[RowParent] | None:
    """Every recorded parent of `row_ordinal`, spine first; None where no sidecar covers it."""
    path = lineage_sidecar_path(run_dir, stage_id)
    # None and [] are different answers: [] is a RECORDED fact — this row has no
    # parents, which a whole-frame aggregate over an empty input emits — while
    # None means nothing was recorded, so the walk must fall back to position.
    if not path.exists():
        return None
    lineage = RowLineage.from_frame(read_frame_file(path))
    if row_ordinal < 0 or row_ordinal >= len(lineage):
        return None
    return list(lineage.parents[row_ordinal])


def _split_spine(hops: list[RowParent]) -> tuple[RowParent | None, list[RowParent]]:
    """The parent the walk follows, and the ones it only reports as branches."""
    # The spine is the first DIRECT parent. Recording puts the subject side
    # first, and where only the reference matched it is the row's only parent —
    # so the spine follows the DATA, not a config default. A contribution parent
    # is never walked into; a row with none therefore has no spine, and the walk
    # ends there with its contributors still reported.
    spine = next((p for p in hops if p.kind == EdgeKind.direct.value), None)
    return spine, [p for p in hops if p is not spine]


def _new_columns(child: pd.DataFrame, parent: pd.DataFrame | None) -> list[str]:
    if parent is None:
        return [str(c) for c in child.columns]
    parent_cols = {str(c) for c in parent.columns}
    return [str(c) for c in child.columns if str(c) not in parent_cols]


def _not_preserving_message(stage_type: str) -> str:
    """The stop message for a stage whose ancestry isn't positionally recoverable."""
    return (f"stops at {stage_type} — it reshapes rows, so row-level lineage "
            "across it needs recording (issue #58)")


def _columns_parent_id(parents: list[str], spine: RowParent | None) -> str | None:
    """The row's actual parent for `columns_new`: the spine, else the single input edge."""
    # A multi-input stage with nothing recorded has no single parent frame to
    # diff against, so every column reads as new there. That overstates what the
    # stage contributed, but the run did not record what it would take to say
    # otherwise, and guessing an input would be worse.
    if spine is not None:
        return spine.stage_id
    return parents[0] if len(parents) == 1 else None


def _advance_via_lineage(
    run_dir: Path, by_id: dict[str, dict[str, Any]], sid: str, spine: RowParent,
) -> tuple[str, int] | TraceEnd:
    """Cross via recorded provenance — valid even where position cannot be trusted."""
    # Checked against the stages in the run, NOT the manifest's input-edge list:
    # that list holds one entry per input that DECLARES a schema, so an
    # undeclared edge is missing from it, while the sidecar names a real edge by
    # construction (the runtime writes it from the stage's own input refs).
    if spine.stage_id not in by_id:
        return TraceEnd(False, sid, "the parent named in lineage is not in the run")
    parent_output = _read_output(run_dir, by_id[spine.stage_id])
    if parent_output is None:
        return TraceEnd(False, spine.stage_id, "this stage's output file is missing from the run")
    if spine.row_ordinal < 0 or spine.row_ordinal >= len(parent_output):
        return TraceEnd(False, sid, "lineage names an out-of-range parent row")
    return spine.stage_id, spine.row_ordinal


def _advance_positionally(
    run_dir: Path, by_id: dict[str, dict[str, Any]], sid: str,
    parent_id: str, r: int, df: pd.DataFrame,
) -> tuple[str, int] | TraceEnd:
    """Cross a row-preserving stage's single input edge, keeping the same ordinal."""
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


def _summarizes_message(contributors: int) -> str:
    # Zero contributors is a recorded fact, not a gap in the recording.
    if not contributors:
        return ("this row summarizes its inputs, and the run recorded that no input "
                "row fed it — an aggregation over an empty group")
    return ("this row summarizes its inputs rather than being made from one "
            "of them — open the contributors to go further")


def _advance(
    run_dir: Path, by_id: dict[str, dict[str, Any]], sid: str, stage_type: str, r: int,
    df: pd.DataFrame, parents: list[str], spine: RowParent | None,
    hops: list[RowParent] | None = None,
) -> tuple[str, int] | TraceEnd:
    """The next `(stage_id, row_ordinal)` to visit, or the `TraceEnd` the walk stops on."""
    # A row-preserving type with the wrong parent arity is treated as NOT
    # preserving — position can't be trusted. Recorded `hops` with no spine stops
    # by DESIGN, not failure: those parents are contributors, reported as branches.
    if stage_type == StageType.input_data:
        return TraceEnd(True, sid, "input_data stage — the rows originate here")
    if spine is not None:
        return _advance_via_lineage(run_dir, by_id, sid, spine)
    if hops is not None:
        return TraceEnd(False, sid, _summarizes_message(len(hops)))
    if not parents:
        return TraceEnd(False, sid, "the manifest records no input edge for this stage")
    # Nothing recorded: the only remaining route is the ordinal, and only where
    # the type guarantees it. A join is NOT crossable this way even though an
    # enrich's output happens to be in subject order — a run made before this
    # recorded lineage stops here, and re-running it is what makes it traceable.
    if not _is_row_preserving(stage_type) or len(parents) != 1:
        return TraceEnd(False, sid, _not_preserving_message(stage_type))
    return _advance_positionally(run_dir, by_id, sid, parents[0], r, df)


def trace_row(run_dir: Path, stage_id: str, row_ordinal: int) -> Trace:
    """One row's ancestry, newest-first, to an origin or the first stage it cannot cross."""
    # Raises only for caller errors (bad stage id / ordinal), never for a state
    # the walk can describe — those come back as a TraceEnd.
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
        hops = _lineage_hops(run_dir, sid, r)
        spine, branches = _split_spine(hops or [])
        columns_parent_id = _columns_parent_id(parents, spine)
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
            branches=branches,
        ))

        next_hop = _advance(
            run_dir, by_id, sid, stage_type, r, df, parents, spine, hops
        )
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
                "branches": [
                    {
                        "stage_id": branch.stage_id,
                        "row_ordinal": branch.row_ordinal,
                        "kind": str(branch.kind),
                        "columns": None if branch.columns is None else list(branch.columns),
                    }
                    for branch in step.branches
                ],
            }
            for step in trace.steps
        ],
        "end": {
            "reached_origin": trace.end.reached_origin,
            "at_stage": trace.end.at_stage,
            "message": trace.end.message,
        },
    }
