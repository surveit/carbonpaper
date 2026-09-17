"""A captured run's own record, and the layout it is written in beside its project archive."""

from __future__ import annotations

from pydantic import BaseModel

from app.core.ids import ID

CAPTURED_ARCHIVE = "project.zip"
CAPTURED_RECORD = "run.json"
CAPTURED_INPUTS = "inputs"


class CapturedInput(BaseModel):
    """One file a stage read, as the run measured it. No path: a restore stages its own."""

    stage_id: ID
    filename: str
    sha256: str
    bytes: int


class CapturedRun(BaseModel):
    # The name, not the id: a restore mints its own project id.
    project_name: str
    run_id: ID
    inputs: list[CapturedInput]
