"""Exceptions raised by the runtime layer."""
from __future__ import annotations

from pathlib import Path


class HaltForReview(Exception):
    """Raised by handle_human_review_queue when there are pending items
    without human decisions. The runner catches this, marks the run as
    awaiting_review, and stops executing downstream stages."""

    def __init__(self, stage_id: str, pending_count: int, queue_path: Path):
        super().__init__(
            f"Stage '{stage_id}' has {pending_count} item(s) awaiting review"
        )
        self.stage_id = stage_id
        self.pending_count = pending_count
        self.queue_path = queue_path


class PreviewError(Exception):
    """Raised when a scratch preview can't be run (bad type, missing upstream
    output, missing handler). The route turns this into a 4xx with the message."""


class RunCancelled(Exception):
    """Raised on the run thread when it consumes a cancel message for its
    (project, run_id); caught by the runner to stop the run. An internal
    control signal — sibling in spirit to HaltForReview
    (app/runtime/errors.py) — never surfaced to a user as an error."""
