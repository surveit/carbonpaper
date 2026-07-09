"""Positional provenance tracer: walk a claim's row back through the
row-preserving stages of one run, by row ordinal alone.

A stage is *row-preserving* when output row i is produced from input row i by
position — true for `input_data` (rows originate here) and `python_row_function`
(a 1:1 map over rows). For such a chain the row ordinal is the cross-stage key,
so nothing needs to be recorded: the tracer just reads row i at each stage. At
any other stage type the walk stops with a reason — `llm_transform` is 1:1 only
once PR #29 lands (issue #61); `join` / `aggregate` / `python_frame_function`
and fan-out reshape rows and need recorded edges (issue #58). The walk also
stops if a supposedly row-preserving hop has unequal row counts on its two
sides, because position cannot be trusted then.

Self-contained on the run directory: reads manifest.json (stage type, parent
edges, row counts) and outputs/<stage>.parquet (row values). It never reads the
compiled DAG, so it is unaffected by later edits to the methodology.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

ROW_PRESERVING: frozenset[str] = frozenset({"input_data", "python_row_function"})

# Why a stage could not be crossed, and the issue that tracks lifting the stop.
STOP_MESSAGES: dict[str, str] = {
    "origin": "input_data stage — the rows originate here",
    "llm_transform": "llm_transform is 1:1 only once PR #29 lands (issue #61)",
    "reshaping": "stage reshapes rows (fan-in/out) — row lineage is issue #58",
    "rowcount_mismatch": (
        "row counts differ across this hop, so position is not trustworthy — "
        "row lineage is issue #58"
    ),
    "missing_output": "this stage's output file is missing from the run",
    "missing_parent": "the parent named in the manifest is not in the run",
    "no_parent_edge": "the manifest records no input edge for this stage",
}


@dataclass
class StopReason:
    kind: str      # a key of STOP_MESSAGES
    stage_id: str  # the stage that could not be crossed (or the origin)
    message: str


@dataclass
class Hop:
    stage_id: str
    stage_type: str
    row_ordinal: int
    row: dict[str, Any]     # the row's cells, verbatim
    columns_new: list[str]  # columns first appearing at this stage vs its parent
    origin: str             # "source" | "computed" | "llm" | "other"


@dataclass
class Trace:
    run_id: str
    start_stage: str
    start_row: int
    hops: list[Hop]         # newest first: start stage, then each ancestor
    terminal: StopReason


def _load_manifest(run_dir: Path) -> dict[str, Any]:
    path = Path(run_dir) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"no manifest.json in {run_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def _stages_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["stage_id"]: s for s in manifest.get("stages", [])}


def _parents(stage_record: dict[str, Any]) -> list[str]:
    parents: list[str] = []
    for entry in stage_record.get("input_validation") or []:
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


def trace_row(run_dir: Path, stage_id: str, row_ordinal: int) -> Trace:
    """Trace one row's ancestry backward through row-preserving stages.

    Returns a `Trace` whose `hops` run newest-first from `(stage_id,
    row_ordinal)` to either an `input_data` origin or the first stage that
    cannot be crossed (`terminal`). Raises `ValueError` for an unknown stage or
    an out-of-range row — those are caller bugs, not traceable states.
    """
    run_dir = Path(run_dir)
    manifest = _load_manifest(run_dir)
    by_id = _stages_by_id(manifest)
    if stage_id not in by_id:
        raise ValueError(f"stage {stage_id!r} not in run {run_dir.name}")

    hops: list[Hop] = []
    sid, r = stage_id, row_ordinal
    terminal: StopReason | None = None

    while terminal is None:
        record = by_id[sid]
        stage_type = record.get("type", "")
        df = _read_output(run_dir, record)
        if df is None:
            terminal = StopReason("missing_output", sid, STOP_MESSAGES["missing_output"])
            break
        if r < 0 or r >= len(df):
            raise ValueError(
                f"row {r} out of range for stage {sid!r} ({len(df)} rows)"
            )

        parents = _parents(record)
        parent_df = None
        if len(parents) == 1 and parents[0] in by_id:
            parent_df = _read_output(run_dir, by_id[parents[0]])

        hops.append(Hop(
            stage_id=sid,
            stage_type=stage_type,
            row_ordinal=r,
            row=_row_dict(df, r),
            columns_new=_new_columns(df, parent_df),
            origin=_origin(stage_type),
        ))

        # Can we cross into the parent, keeping the same ordinal?
        if stage_type == "input_data":
            terminal = StopReason("origin", sid, STOP_MESSAGES["origin"])
        elif not parents:
            terminal = StopReason("no_parent_edge", sid, STOP_MESSAGES["no_parent_edge"])
        elif stage_type not in ROW_PRESERVING:
            kind = "llm_transform" if stage_type == "llm_transform" else "reshaping"
            terminal = StopReason(kind, sid, STOP_MESSAGES[kind])
        elif len(parents) != 1:
            # A row-preserving stage has exactly one input; more means the
            # manifest is mislabeled — treat as reshaping, don't guess a parent.
            terminal = StopReason("reshaping", sid, STOP_MESSAGES["reshaping"])
        else:
            parent_id = parents[0]
            if parent_id not in by_id:
                terminal = StopReason("missing_parent", sid, STOP_MESSAGES["missing_parent"])
            elif parent_df is None:
                terminal = StopReason("missing_output", parent_id, STOP_MESSAGES["missing_output"])
            elif len(parent_df) != len(df):
                terminal = StopReason("rowcount_mismatch", sid, STOP_MESSAGES["rowcount_mismatch"])
            else:
                sid, r = parent_id, r  # same ordinal — the whole point

    return Trace(
        run_id=manifest.get("run_id", run_dir.name),
        start_stage=stage_id,
        start_row=row_ordinal,
        hops=hops,
        terminal=terminal,
    )


def trace_to_dict(trace: Trace) -> dict[str, Any]:
    """Flatten a Trace to a JSON-able nested dict for the API and templates."""
    return {
        "run_id": trace.run_id,
        "start_stage": trace.start_stage,
        "start_row": trace.start_row,
        "hops": [
            {
                "stage_id": hop.stage_id,
                "stage_type": hop.stage_type,
                "row_ordinal": hop.row_ordinal,
                "row": hop.row,
                "columns_new": hop.columns_new,
                "origin": hop.origin,
            }
            for hop in trace.hops
        ],
        "terminal": {
            "kind": trace.terminal.kind,
            "stage_id": trace.terminal.stage_id,
            "message": trace.terminal.message,
        },
    }
