"""What a caller DECIDED about one run, as opposed to what the run then did. The
test for membership: a resume has to replay it. Carried on `RunContext.params` and
recorded verbatim on `RunManifest.parameters`, so the settings a run executed under
and the settings it records are one object."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride


class RunParameters(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Per-stage row window. For a stage with inputs the cut is taken on its INPUT
    # frames; a stage with no inputs has none, so it is taken on the frame it loads.
    limits: dict[str, int] = {}
    offsets: dict[str, int] = {}
    # Skip every stage-cache READ; a write-capable accessor still records what it
    # computes, so the cache ends the run re-pinned rather than stale.
    bust_cache: bool = False
    # A human_review_queue stage approves every row in memory instead of halting.
    queue_auto_approve: bool = False
    # Not a production run: excluded from a project's run counts and its latest run.
    is_test_run: bool = False
    # Per-run connector params, keyed by stage id, merged over a stage's authored
    # params for this run only.
    run_bindings: dict[StageId, TypeUnsafeUserStageConfigOverride] = {}

    @model_validator(mode="after")
    def _only_a_test_run_may_auto_approve(self) -> RunParameters:
        if self.queue_auto_approve and not self.is_test_run:
            raise ValueError(
                "queue_auto_approve is set on a run that is not marked is_test_run — "
                "its in-memory approvals would read back as human decisions. Mark the "
                "run a test run, or let its queue halt for a human."
            )
        return self
