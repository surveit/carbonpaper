"""
In-memory scratch re-run of a single stage.

This module powers the node-detail panel's "Run transform on selected" button.
It loads a stage's upstream inputs from a completed run's on-disk outputs,
subsets them to a caller-chosen set of row indices, runs THIS stage's handler
in memory, and returns the resulting rows as records.

Hard guarantee: NOTHING is persisted. No manifest is touched, no output parquet
is written, no queue snapshot or artifact is produced. We achieve this by
calling the stage handler directly (never the runner) and by refusing the two
stage types whose handlers have disk side-effects:

  - human_review_queue  → writes a queue snapshot + raises HaltForReview
  - publish             → writes artifacts to run_dir/artifacts

Allowed types are the pure transforms: python_transform, llm_transform, join,
aggregate. (input_data is also refused — it has no upstream rows to subset.)

This module imports and reuses the existing handlers; it does not change their
behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .handlers import HANDLERS
from .runner import get_input_id


# Stage types whose handlers are pure (no disk writes) and therefore safe to
# run as an ephemeral scratch preview.
PREVIEWABLE_TYPES: set[str] = {
    "python_transform",
    "llm_transform",
    "join",
    "aggregate",
}


class PreviewError(Exception):
    """Raised when a scratch preview can't be run (bad type, missing upstream
    output, missing handler). The route turns this into a 4xx with the message."""


def _read_output(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def _load_upstream_inputs(
    stage_def: dict[str, Any],
    run_dir: Path,
    output_by_id: dict[str, str | None],
) -> dict[str, pd.DataFrame]:
    """Load each declared upstream input's output dataframe from this run's
    on-disk outputs. Raises PreviewError if any upstream output is missing."""
    inputs: dict[str, pd.DataFrame] = {}
    for inp in stage_def.get("inputs", []) or []:
        iid = get_input_id(inp)
        rel = output_by_id.get(iid)
        if not rel:
            raise PreviewError(
                f"upstream input '{iid}' has no output in this run — cannot preview"
            )
        path = run_dir / rel
        if not path.exists():
            raise PreviewError(f"upstream output for '{iid}' missing on disk: {rel}")
        try:
            inputs[iid] = _read_output(path)
        except Exception as exc:  # noqa: BLE001
            raise PreviewError(f"could not read upstream '{iid}': {exc}") from exc
    return inputs


def run_stage_preview(
    *,
    stage_def: dict[str, Any],
    run_dir: Path,
    repo_root: Path,
    methodology_dir: Path,
    output_by_id: dict[str, str | None],
    selected_indices: list[int],
) -> dict[str, Any]:
    """Run `stage_def`'s handler on the chosen rows of its FIRST upstream input,
    entirely in memory, and return the output as records.

    `selected_indices` are positional row indices (0-based) into the first
    upstream input's dataframe — the same rows the panel shows in its input
    preview. Other upstream inputs (e.g. the right side of a join) are passed
    through whole, since "row N of a join" isn't well defined.

    Returns a dict: {columns, rows_total, preview (records), input_rows,
    truncated_to}. Never writes to disk.
    """
    stype = stage_def.get("type")
    if stype not in PREVIEWABLE_TYPES:
        raise PreviewError(
            f"stage type '{stype}' can't be previewed in memory "
            f"(allowed: {sorted(PREVIEWABLE_TYPES)})"
        )
    handler = HANDLERS.get(stype)
    if handler is None:
        raise PreviewError(f"no handler registered for type '{stype}'")

    inputs = _load_upstream_inputs(stage_def, run_dir, output_by_id)

    declared_inputs = stage_def.get("inputs", []) or []
    if not declared_inputs:
        raise PreviewError("stage has no declared inputs to subset")
    first_id = get_input_id(declared_inputs[0])
    base_df = inputs[first_id]

    # Subset the FIRST input to the chosen positional indices. Clamp to range,
    # drop dupes, preserve caller order.
    n = len(base_df)
    seen: set[int] = set()
    valid: list[int] = []
    for i in selected_indices:
        if isinstance(i, int) and 0 <= i < n and i not in seen:
            seen.add(i)
            valid.append(i)
    if not valid:
        raise PreviewError(
            f"no valid row indices selected (input has {n} row(s))"
        )
    inputs[first_id] = base_df.iloc[valid].reset_index(drop=True)

    # Ephemeral context. We pass the real run_dir/methodology_dir for read-only
    # path resolution, but a pure handler (python/llm/join/aggregate) never
    # writes — and we never call the runner, so no manifest/output is touched.
    ctx: dict[str, Any] = {
        "repo_root": repo_root,
        "run_dir": run_dir,
        "methodology_dir": methodology_dir,
        "queue_stats": {},
        "_scratch_preview": True,
    }

    output = handler(stage_def, inputs, ctx)
    if output is None:
        output = pd.DataFrame()

    safe = output.fillna("").astype(str)
    return {
        "columns": list(output.columns),
        "rows_total": int(len(output)),
        "input_rows": len(valid),
        "selected_indices": valid,
        "preview": safe.to_dict(orient="records"),
    }
