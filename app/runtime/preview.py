"""In-memory scratch re-run of a single stage.

Hard guarantee: NOTHING is persisted. Handlers are called directly, never the
runner, and the two stage types whose handlers touch disk (human_review_queue,
publish) are refused - as is input_data, which has no upstream rows to subset.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow

from app.core.frames import PARQUET_SUFFIX
from app.models import Stage

from .context import RunContext
from .errors import PreviewError
from .stages import HANDLERS


# Stage types whose handlers are pure (no disk writes) and therefore safe to
# run as an ephemeral scratch preview.
PREVIEWABLE_TYPES: set[str] = {
    "python_row_function",
    "python_frame_function",
    "llm_transform",
    "join",
    "aggregate",
}


def _read_output(path: Path) -> pd.DataFrame:
    if path.suffix == PARQUET_SUFFIX:
        return pd.read_parquet(path)
    return pd.read_csv(path)


def _load_upstream_inputs(
    stage_def: Stage,
    run_dir: Path,
    output_by_id: dict[str, str | None],
) -> dict[str, pd.DataFrame]:
    """Load each declared upstream input's output dataframe from this run's
    on-disk outputs. Raises PreviewError if any upstream output is missing."""
    inputs: dict[str, pd.DataFrame] = {}
    for ref in stage_def.inputs:
        iid = ref.id
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
        except (OSError, ValueError, pyarrow.lib.ArrowException) as exc:
            raise PreviewError(f"could not read upstream '{iid}': {exc}") from exc
    return inputs


def run_stage_preview(
    *,
    stage_def: Stage,
    run_dir: Path,
    repo_root: Path,
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
    stype = stage_def.type
    if stype not in PREVIEWABLE_TYPES:
        raise PreviewError(
            f"stage type '{stype}' can't be previewed in memory "
            f"(allowed: {sorted(PREVIEWABLE_TYPES)})"
        )
    handler = HANDLERS.get(stype)
    if handler is None:
        raise PreviewError(f"no handler registered for type '{stype}'")

    inputs = _load_upstream_inputs(stage_def, run_dir, output_by_id)

    declared_inputs = stage_def.inputs
    if not declared_inputs:
        raise PreviewError("stage has no declared inputs to subset")
    first_id = declared_inputs[0].id
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

    # Ephemeral context: no identity/stage_cache (this run has no project
    # scope), and a pure handler (python/llm/join/aggregate) never writes — we
    # never call the runner, so no manifest/output is touched.
    ctx = RunContext.for_non_production_run(repo_root, run_dir)

    output = handler.execute(stage_def, inputs, ctx)
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
