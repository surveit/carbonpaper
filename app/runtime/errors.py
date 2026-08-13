"""Exceptions raised by the runtime layer."""
from __future__ import annotations

from pathlib import Path

from app.models.run_manifest import StageContribution


class HaltForReview(Exception):
    """Control signal, not a failure — and the return path for `contribution`, no frame being returned."""

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
    pass


class RunCancelled(Exception):
    """An internal control signal the runner catches to stop the run; never shown as an error."""


# Raised BEFORE the frame is coerced to arrow, so the caller can tell an authored
# function that returned the wrong thing from one that refused: a refusal raises
# StepRefused and satisfies an expected-failure test, this does not.
class AuthoredFrameExpected(TypeError):
    """An authored `transform`/publish function returned something other than a DataFrame."""

    def __init__(self, message: str, returned: str) -> None:
        super().__init__(message)
        # The type name alone, so a caller can report the return without the stage prefix.
        self.returned = returned
