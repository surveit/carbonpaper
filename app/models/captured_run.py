"""What a capture records beside its archive: the run's own settings."""

from __future__ import annotations

from pydantic import BaseModel

from app.models.run_parameters import RunParameters


class CapturedRun(BaseModel):
    # Only the three a restore replays; the rest name this workspace's own project.
    parameters: RunParameters
