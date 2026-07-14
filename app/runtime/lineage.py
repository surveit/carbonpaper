"""Row-level lineage substrate for row-reshaping stages.

Row-preserving stages (``input_data`` origin, ``python_row_function``,
``llm_transform``) need no recording: output row *i* traces to input row *i*
by position, so the row ordinal is the cross-stage key (see the show-your-work
design, §4.3). Recording only earns its keep at a stage that *reshapes* rows —
where output-row position no longer equals input-row position: ``join``
(fan-out), ``aggregate`` (fan-in), ``human_review_queue`` (drops/reorders), and
the opaque ``python_frame_function``.

For those, the handler records **edges** — one per (output row, contributing
input row) pair — into the run context. The runner then re-slices those edges
through the SAME offset/limit slice it applies to the stage output and persists
them as a per-stage sidecar ``lineage/<stage_id>.parquet`` with columns
``out_row, in_stage, in_row`` (0-based ordinals in the *persisted* output and in
each named upstream stage's output).

An opaque frame function whose edges cannot be recovered is recorded as
``untracked`` (a marker sidecar) rather than silently pretending its rows are
positionally aligned — the tracer must be able to tell "no lineage here" apart
from "lineage says row i came from row j".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

# A recorded edge: output-row ordinal, contributing upstream stage id, that
# stage's output-row ordinal.
Edge = tuple[int, str, int]

LINEAGE_COLUMNS = ["out_row", "in_stage", "in_row"]

# Sentinel stored in ctx["lineage"][stage_id] for a reshaping stage whose edges
# could not be recovered (an opaque frame function). Persisted as a marker
# sidecar so the tracer stops with an honest "untracked" rather than guessing.
UNTRACKED = "untracked"

_UNTRACKED_SUFFIX = ".untracked"


def record_edges(ctx: dict[str, Any], stage_id: str, edges: list[Edge]) -> None:
    """Stash a reshaping stage's recovered edges on the run context, keyed by
    stage id. The runner picks them up after it applies the output slice,
    re-aligns them with :func:`slice_edges`, and writes the sidecar."""
    ctx.setdefault("lineage", {})[stage_id] = list(edges)


def record_untracked(ctx: dict[str, Any], stage_id: str) -> None:
    """Mark a reshaping stage's lineage as unrecoverable (opaque frame
    function). Persisted as a marker sidecar so the tracer reports 'untracked'
    instead of falling back to a wrong positional identity."""
    ctx.setdefault("lineage", {})[stage_id] = UNTRACKED


def slice_edges(edges: list[Edge], offset: int, limit: int | None) -> list[Edge]:
    """Re-align edges recorded on *pre-slice* output ordinals to the output the
    runner actually persists.

    The runner slices each stage's output with ``output.iloc[offset:]
    .reset_index(drop=True)`` then ``.head(limit)`` (offset dropped first, then
    the cap). Edges carry the handler's pre-slice ``out_row``; this mirrors the
    exact same slice on them: drop any edge whose row fell before the offset or
    at/after the cap, and renumber the survivors so ``out_row`` again matches
    the persisted row position.

    ``offset`` is the number of leading rows dropped (0 if none); ``limit`` is
    the retained-row cap after the offset (``None`` if uncapped).
    """
    sliced: list[Edge] = []
    for out_row, in_stage, in_row in edges:
        shifted = out_row - offset
        if shifted < 0:
            continue
        if limit is not None and shifted >= limit:
            continue
        sliced.append((shifted, in_stage, in_row))
    return sliced


def _lineage_dir(run_dir: Path) -> Path:
    d = run_dir / "lineage"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_lineage(run_dir: Path, stage_id: str, edges: list[Edge]) -> Path:
    """Persist a stage's (already-sliced) edges as ``lineage/<stage_id>.parquet``.
    An empty edge list still writes an empty, correctly-typed frame so the
    tracer can distinguish 'recorded, no surviving edges' from 'never recorded'.
    """
    d = _lineage_dir(run_dir)
    df = pd.DataFrame(edges, columns=LINEAGE_COLUMNS)
    if df.empty:
        df = df.astype({"out_row": "int64", "in_stage": "object", "in_row": "int64"})
    path = d / f"{stage_id}.parquet"
    df.to_parquet(path, index=False)
    return path


def write_untracked(run_dir: Path, stage_id: str) -> Path:
    """Persist the 'lineage unrecoverable' marker for a reshaping stage."""
    d = _lineage_dir(run_dir)
    path = d / f"{stage_id}{_UNTRACKED_SUFFIX}"
    path.write_text("untracked\n", encoding="utf-8")
    return path


def persist_stage_lineage(
    run_dir: Path,
    stage_id: str,
    recorded: list[Edge] | str,
    offset: int,
    limit: int | None,
) -> str | None:
    """Persist whatever a handler recorded for one stage, re-sliced to the
    persisted output. Returns a manifest-friendly note ('untracked' or the
    sidecar's relative path), or None if nothing was recorded."""
    if recorded == UNTRACKED:
        path = write_untracked(run_dir, stage_id)
        return str(path.relative_to(run_dir))
    assert isinstance(recorded, list)
    sliced = slice_edges(recorded, offset, limit)
    path = write_lineage(run_dir, stage_id, sliced)
    return str(path.relative_to(run_dir))


def read_lineage(run_dir: Path, stage_id: str) -> pd.DataFrame | None:
    """Load a stage's lineage sidecar, or None if none was recorded."""
    path = run_dir / "lineage" / f"{stage_id}.parquet"
    if not path.exists():
        return None
    return pd.read_parquet(path)


def is_untracked(run_dir: Path, stage_id: str) -> bool:
    """True if the stage recorded an 'untracked' marker (unrecoverable lineage)."""
    return (run_dir / "lineage" / f"{stage_id}{_UNTRACKED_SUFFIX}").exists()
