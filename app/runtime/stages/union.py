"""Handler for the union stage type: concatenates its declared inputs, in
declared order. Schema-equality across inputs is enforced at stage-validation
time (app.models.stages.union), so nothing here checks it again. Row provenance
is NOT computed here; the runtime has it (app.runtime.lineage).
"""
from __future__ import annotations

import pyarrow as pa

from app.core.frames import concat_tables
from app.models import Stage

from ..context import RunContext
from ..stage_output import StageOutput


def handle_union(stage: Stage, inputs: dict[str, pa.Table], ctx: RunContext) -> StageOutput:
    tables = [inputs[ref.id] for ref in stage.inputs]
    return StageOutput(concat_tables(tables))
