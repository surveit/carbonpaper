"""Typed reader for a run's on-disk `manifest.json`.

The runner (`app.runtime.runner`) writes a JSON manifest summarising a run; this
module parses the subset the authoring loop reads — the run id/status/version and
per-stage row counts and model usage — while ignoring the many bookkeeping keys
it does not need. It is a read model only: it imports `LlmUsage` from
app.core.agent (allowed: models may depend on app.core) and never touches
app.runtime, so services can read run outcomes without driving the runner.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agent.usage import LlmUsage


class StageRunRecord(BaseModel):
    """One stage's line in a run manifest: its id, terminal status, output row
    count, and model usage when the stage made LLM calls (absent -> None). The
    manifest names the row count `rows`; it is read under that alias."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    stage_id: str
    status: str
    row_count: int | None = Field(default=None, alias="rows")
    llm_usage: LlmUsage | None = None


class RunManifest(BaseModel):
    """The subset of a run's `manifest.json` the authoring loop consumes. Extra
    manifest keys are tolerated so this stays valid as the runner's manifest
    grows fields the loop does not read."""

    model_config = ConfigDict(extra="ignore")

    run_id: str
    status: Literal[
        "running", "ok", "warnings", "errors", "awaiting_review", "corrupt"
    ]
    workflow_version: str
    stages: list[StageRunRecord]

    def total_usage(self) -> LlmUsage:
        """Field-wise total model usage across the run's stages (stages with no
        usage contribute the zero instance)."""
        return LlmUsage.summed(
            stage.llm_usage for stage in self.stages if stage.llm_usage is not None
        )
