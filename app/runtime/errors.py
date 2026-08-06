"""Exceptions raised by the runtime layer."""
from __future__ import annotations

from pathlib import Path

from app.models.run_manifest import StageContribution


class HaltForReview(Exception):
    """Raised by the queue row mapper's post-map step
    (app/runtime/stages/human_review_queue.py) when a queue stage's rows include
    ones no human has decided yet. Carries `stage_id`, the stage that has pending
    items; `pending_count`, how many; and `queue_path`, the snapshot file those
    rows were written to. An internal control signal, not a user-facing error —
    nothing failed.

    Carries the stage's `contribution` (its queue stats) because the halt fires
    before the handler returns a frame — so this exception is the return path
    the executor merges into the manifest, exactly as it merges a returned
    frame's `.attrs` on the non-halt path."""

    def __init__(
        self,
        stage_id: str,
        pending_count: int,
        queue_path: Path,
        contribution: StageContribution,
    ):
        super().__init__(
            f"Stage '{stage_id}' has {pending_count} item(s) awaiting review"
        )
        self.stage_id = stage_id
        self.pending_count = pending_count
        self.queue_path = queue_path
        self.contribution = contribution


class PreviewError(Exception):
    """Raised when a scratch preview can't be run (bad type, missing upstream
    output, missing handler). The route turns this into a 4xx with the message."""


class RunCancelled(Exception):
    """Raised on the run thread when it consumes a cancel message for its
    (project, run_id); caught by the runner to stop the run. An internal
    control signal — sibling in spirit to HaltForReview
    (app/runtime/errors.py) — never surfaced to a user as an error."""
