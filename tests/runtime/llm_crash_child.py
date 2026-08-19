"""One llm_transform execution in a fresh process, against a real on-disk store.

`kill <n>` SIGKILLs itself once the n-th model call has been answered — the OOM
kill / machine stop an in-process test cannot stage. `replay` re-runs the same
stage and prints the calls it still had to make.
"""
from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

import pandas as pd

from app.core.store_config import configure_default_stores
from app.core.stage_cache import StageCache
from app.models import parse_stage
from app.models.run_parameters import RunParameters
from app.models.stage import StageType
from app.models.schema import TableSchema
from app.models import WorkflowStage, WorkflowStageInput
from app.core.frames import frame_to_table
from app.runtime.context import RunContext, RunIdentity
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt

PROJECT = "llm-crash"
ROWS = 8
_SRC = pd.DataFrame({"post_id": [f"p{i}" for i in range(ROWS)], "text": [f"t{i}" for i in range(ROWS)]})
_LOAD_COLUMNS = [{"name": "post_id", "type": "str", "nullable": True},
                 {"name": "text", "type": "str", "nullable": True}]


def _stage(batch_size: int):
    return parse_stage({
        "id": "score", "description": "Score", "type": "llm_transform",
        "inputs": [{"id": "load"}], "cache": True,
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": [_LOAD_COLUMNS[1]]}],
            "adds": [{"name": "label", "type": "str", "nullable": True}]},
        "llm": {"prompt_data_template": "score {text}", "batch_size": batch_size,
                "max_retries": 0},
    })


def _placed(batch_size: int) -> WorkflowStage:
    stage = _stage(batch_size)
    inputs = [WorkflowStageInput(id="load", table_schema=TableSchema.model_validate(
        {"columns": _LOAD_COLUMNS}))]
    adds = [{"name": "label", "type": "str", "nullable": True}]
    return WorkflowStage(stage=stage, inputs=inputs,
                         output_schema=TableSchema.model_validate({"columns": [*_LOAD_COLUMNS, *adds]}))


def main() -> None:
    mode, batch_size = sys.argv[1], int(sys.argv[2])
    kill_after = int(sys.argv[3]) if mode == "kill" else 0
    probe = Path(os.environ["CRASH_PROBE"])
    configure_default_stores()

    def fake_call(*args, **kwargs):
        with probe.open("a", encoding="utf-8") as handle:
            handle.write(f"{mode}\n")
        answered = sum(1 for line in probe.read_text(encoding="utf-8").splitlines() if line == mode)
        if mode == "kill" and answered == kill_after:
            os.kill(os.getpid(), signal.SIGKILL)
        if "task" in kwargs:
            k = kwargs["task"].count("### item ")
            return {"results": [{"row_number": i, "label": f"L{i}"} for i in range(k)]}
        return {"label": "L"}

    lt.call_llm = fake_call
    lt.call_llm_batch = fake_call
    handler = HANDLERS[StageType.llm_transform]
    handler.parallelism = 1
    ctx = RunContext(
        run_dir=Path("."), identity=RunIdentity(project=PROJECT, run_id=mode),
        stage_cache=StageCache(), params=RunParameters())
    handler.execute(_placed(batch_size), {"load": frame_to_table(_SRC)}, ctx)


if __name__ == "__main__":
    main()
