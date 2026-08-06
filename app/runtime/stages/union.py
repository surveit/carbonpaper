"""Handler for the union stage type: concatenates its declared inputs, in
declared order, via `app.core.frames.concat_frames` — at the Arrow layer, so a
list cell reads as a `list` whichever input it came from. Schema-equality across
inputs is enforced at stage-validation time (app.models.stages.union). Row
provenance is NOT computed here; the runtime has it (app.runtime.lineage)."""
from __future__ import annotations

import pandas as pd

from app.core.frames import concat_frames
from app.models import Stage

from ..context import RunContext


def handle_union(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    return concat_frames([inputs[ref.id] for ref in stage.inputs])
