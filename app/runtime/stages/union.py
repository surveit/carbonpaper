"""Handler for the union stage type: concatenates its declared inputs, in
declared order. Schema-equality across inputs is enforced at stage-validation
time (app.models.stages.union), so nothing here needs to check it again. Row
provenance is NOT computed here — declared-order concatenation means the input
row counts already carry it, and the runtime has those (app.runtime.lineage)."""
from __future__ import annotations

import pandas as pd

from app.models import WorkflowStage

from ..context import RunContext


def handle_union(
    workflow_stage: WorkflowStage, inputs: dict[str, pd.DataFrame], ctx: RunContext
) -> pd.DataFrame:
    frames = [inputs[ref.id] for ref in workflow_stage.inputs]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
