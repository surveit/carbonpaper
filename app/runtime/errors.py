"""Exceptions raised by the runtime layer."""
from __future__ import annotations


class PreviewError(Exception):
    pass


class RunCancelled(Exception):
    """An internal control signal the runner catches to stop the run; never shown as an error."""


# Raised BEFORE the frame is coerced to arrow, so the caller can tell an authored
# function that returned the wrong thing from one that refused: a refusal raises
# StepRefused and satisfies an expected-failure test, this does not.
class AuthoredFrameExpected(TypeError):
    """An authored `transform`/report function returned something other than a DataFrame."""

    def __init__(self, message: str, returned: str) -> None:
        super().__init__(message)
        # The type name alone, so a caller can report the return without the stage prefix.
        self.returned = returned


class BranchRecordingError(RuntimeError):
    """The driver mispaired a row; a branch would be attributed to the wrong one."""


class MissingLineage(RuntimeError):
    """A stage owed a lineage sidecar by its type's contract and wrote none."""


class NotALoadStage(RuntimeError):
    """A stage with no inputs that is not an input_data stage."""


class LineageSidecarLengthMismatch(RuntimeError):
    """The two halves of a stage's row sidecar disagree on how many rows it has."""
