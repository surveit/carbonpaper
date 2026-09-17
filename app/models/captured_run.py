"""A captured run's own record, written beside the project archive it travels with."""

from __future__ import annotations

from pydantic import BaseModel

from app.core.ids import ID
from app.models.run_manifest import ReadFile


class CapturedInput(BaseModel):
    """`file` is the run's own measurement, so a restore can refuse bytes that moved."""

    stage_id: ID
    file: ReadFile


class CapturedRun(BaseModel):
    # The name, not the id: a restore mints its own project id.
    project_name: str
    run_id: ID
    inputs: list[CapturedInput]
