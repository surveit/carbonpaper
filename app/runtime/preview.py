"""In-memory re-run of one stage. NOTHING is persisted, so disk-touching types are refused."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow

from app.core.frames import frame_to_table, read_frame_file, table_to_frame
from app.models import WorkflowStage

from .context import RunContext
from .errors import PreviewError
from .stages import HANDLERS


# Stage types whose handlers are pure (no disk writes) and therefore safe to
# run as an ephemeral scratch preview.
PREVIEWABLE_TYPES: set[str] = {
    "python_row_function",
    "python_frame_function",
    "starlark_row_function",
    "llm_transform",
    "enrich",
    "expand",
    "aggregate",
}


def _load_upstream_inputs(
    workflow_stage: WorkflowStage,
    run_dir: Path,
    output_by_id: dict[str, str | None],
) -> dict[str, pd.DataFrame]:
    inputs: dict[str, pd.DataFrame] = {}
    for ref in workflow_stage.inputs:
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
            inputs[iid] = read_frame_file(path)
        except (OSError, ValueError, pyarrow.lib.ArrowException) as exc:
            raise PreviewError(f"could not read upstream '{iid}': {exc}") from exc
    return inputs


@dataclass(frozen=True)
class StagePreview:
    frame: pd.DataFrame
    input_rows: int
    selected_indices: list[int]


def run_stage_preview(
    *,
    workflow_stage: WorkflowStage,
    run_dir: Path,
    output_by_id: dict[str, str | None],
    selected_indices: list[int],
) -> StagePreview:
    """`selected_indices` are 0-based positional; upstream inputs after the first pass through whole."""
    stype = workflow_stage.stage.type
    if stype not in PREVIEWABLE_TYPES:
        raise PreviewError(
            f"stage type '{stype}' can't be previewed in memory "
            f"(allowed: {sorted(PREVIEWABLE_TYPES)})"
        )
    handler = HANDLERS.get(stype)
    if handler is None:
        raise PreviewError(f"no handler registered for type '{stype}'")

    inputs = _load_upstream_inputs(workflow_stage, run_dir, output_by_id)

    declared_inputs = workflow_stage.inputs
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
    ctx = RunContext.for_stages_outside_a_run(run_dir)

    output = handler.execute(
        workflow_stage, {name: frame_to_table(f) for name, f in inputs.items()}, ctx)
    frame = pd.DataFrame() if output is None else table_to_frame(output.table)

    return StagePreview(frame=frame, input_rows=len(valid), selected_indices=valid)
