"""Handler for the union stage type: concatenates its declared inputs, in
declared order. Schema-equality across inputs is enforced at stage-validation
time (app.models.stages.union), so nothing here needs to check it again."""
from __future__ import annotations

import pandas as pd

from app.models import Stage

from ..context import RunContext
from .lineage import attach_row_provenance


def handle_union(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    frames = [inputs[ref.id] for ref in stage.inputs]
    source_stage: list[str] = []
    source_row: list[int] = []
    for ref, frame in zip(stage.inputs, frames):
        source_stage.extend([ref.id] * len(frame))
        source_row.extend(range(len(frame)))
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return attach_row_provenance(combined, source_stage, source_row)
