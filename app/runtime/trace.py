"""Row-level lineage tracer — the "show your work" read side.

Given a persisted run and a (stage, output-row) pair, walk backwards to the
input rows it derives from. Two kinds of hop:

* **Row-preserving stages** (``input_data`` origin, ``python_row_function``,
  ``llm_transform``) record no sidecar: output row *i* is input row *i* by
  position, so the tracer follows the ordinal straight through.
* **Row-reshaping stages** (``join``, ``aggregate``, ``human_review_queue``,
  and recoverable ``python_frame_function``) record a ``lineage/<stage>.parquet``
  sidecar of ``out_row, in_stage, in_row`` edges; the tracer consumes it instead
  of stopping. An opaque frame function that could not be recovered leaves an
  ``untracked`` marker, and the tracer reports that honestly rather than
  guessing a positional identity a reshape may have broken.

The v1 tracer stopped at the first reshaping stage and pointed at this work;
this module is that follow-through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models import Stage

from .lineage import is_untracked, read_lineage


def _parents(run_dir: Path, stage: Stage, out_row: int) -> list[tuple[str, int]] | None:
    """Immediate (in_stage, in_row) parents of one output row of ``stage``.

    Returns:
      * ``[]``  — this row originates here (``input_data``), or a recorded
        sidecar has no surviving edge for it; a dead end either way.
      * ``None`` — lineage is unavailable (``untracked`` marker, or a reshaping
        stage with no recording); the caller marks the branch untracked.
      * a list of parents otherwise.
    """
    sidecar = read_lineage(run_dir, stage.id)
    if sidecar is not None:
        hits = sidecar[sidecar["out_row"] == out_row]
        in_stages = hits["in_stage"].tolist()
        in_rows = hits["in_row"].tolist()
        return [(str(s), int(i)) for s, i in zip(in_stages, in_rows)]

    if is_untracked(run_dir, stage.id):
        return None

    if not stage.inputs:
        return []  # origin (input_data)

    # No sidecar: for a row-preserving stage the ordinal passes straight through
    # its single input. A reshaping stage with no recording can't be followed.
    if stage.is_grain_preserving and len(stage.inputs) == 1:
        return [(stage.inputs[0].id, out_row)]
    return None


def trace_row(
    run_dir: Path,
    stages_by_id: dict[str, Stage],
    stage_id: str,
    out_row: int,
) -> dict[str, Any]:
    """Build the lineage tree for one output row, back to origin rows.

    Each node is ``{stage, row, type, origin, untracked, sources}``. ``origin``
    marks an ``input_data`` leaf; ``untracked`` marks a branch where lineage
    ran out (opaque reshape). ``sources`` are the recursively-traced parents.
    """
    return _trace(run_dir, stages_by_id, stage_id, out_row, seen=set())


def _trace(
    run_dir: Path,
    stages_by_id: dict[str, Stage],
    stage_id: str,
    out_row: int,
    seen: set[tuple[str, int]],
) -> dict[str, Any]:
    stage = stages_by_id.get(stage_id)
    node: dict[str, Any] = {
        "stage": stage_id,
        "row": out_row,
        "type": stage.type if stage is not None else None,
        "origin": False,
        "untracked": False,
        "sources": [],
    }
    if stage is None:
        # Referenced upstream stage isn't in the graph we were handed — can't go on.
        node["untracked"] = True
        return node

    key = (stage_id, out_row)
    if key in seen:
        return node  # DAG guard; should not recur, but never loop.
    seen = seen | {key}

    parents = _parents(run_dir, stage, out_row)
    if parents is None:
        node["untracked"] = True
        return node
    if not parents:
        node["origin"] = not stage.inputs  # input_data with no upstream
        return node

    node["sources"] = [
        _trace(run_dir, stages_by_id, in_stage, in_row, seen)
        for in_stage, in_row in parents
    ]
    return node


def trace_to_origins(
    run_dir: Path,
    stages_by_id: dict[str, Stage],
    stage_id: str,
    out_row: int,
) -> dict[str, Any]:
    """Flatten :func:`trace_row` to its leaves: the origin ``(stage, row)`` pairs
    a row derives from, plus whether any branch dead-ended as ``untracked``.

    Returns ``{"origins": [(stage_id, row), ...], "untracked": bool}`` with
    origins de-duplicated in first-seen order.
    """
    tree = trace_row(run_dir, stages_by_id, stage_id, out_row)
    origins: list[tuple[str, int]] = []
    untracked = False

    def walk(node: dict[str, Any]) -> None:
        nonlocal untracked
        if node["untracked"]:
            untracked = True
        if node["origin"]:
            pair = (node["stage"], node["row"])
            if pair not in origins:
                origins.append(pair)
        for child in node["sources"]:
            walk(child)

    walk(tree)
    return {"origins": origins, "untracked": untracked}
