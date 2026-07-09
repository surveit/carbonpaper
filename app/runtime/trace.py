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
