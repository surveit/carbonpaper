"""external stage: the command it runs per row, and the standing caveat the
stage panel states about every stage of this type.
"""
from __future__ import annotations

import shutil
from typing import ClassVar, Literal

from pydantic import Field, field_validator

from app.models.schema import StageConfig
from app.models.stage_base import StageBase, StageInput, StageType

# What the stage panel states about every external stage. One place, so the page
# and the authoring prompt cannot say different things.
NOT_REPRODUCIBLE_NOTE = (
    "This stage is neither reproducible nor reviewable from this page. Its output "
    "depends on what it reached outside the workflow while the run was happening, "
    "which no later run can be held to, and it runs as a separate program this "
    "page cannot show. Nothing here can be checked against an authored example."
)


class ExternalConfig(StageConfig):
    """external handle: the argv spawned once per row, and the timeout that kills it."""

    # Both fields fix what runs and how long it may run, so both change what this
    # stage computes — see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"command", "timeout_seconds"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    command: list[str] = Field(
        min_length=1,
        description=(
            "REQUIRED: the program and its arguments as an argv LIST, e.g. "
            '["python", "-m", "my_capture"]. Never a shell string — no shell runs '
            "it, so there is nothing to quote, word-split or inject into, and every "
            "element reaches the program exactly as written. The runtime spawns it "
            "once per input row, writes that row to its stdin as one JSON object, "
            "closes stdin, and reads one JSON object back from its stdout. "
            "`command[0]` must resolve to an executable — findable on PATH, or a "
            "path that exists — when the stage is saved."
        ),
    )
    timeout_seconds: int = Field(
        gt=0,
        description=(
            "REQUIRED, positive: how long ONE row's process may run before the "
            "runtime kills it and fails the stage. There is no default — code that "
            "reaches the outside world has no safe one, and a number chosen here "
            "would be invented rather than measured."
        ),
    )

    @field_validator("command")
    @classmethod
    def _argv_is_runnable(cls, command: list[str]) -> list[str]:
        if any(not part for part in command):
            raise ValueError(
                "external.command carries an empty argv element — every element is "
                "passed to the program verbatim, so an empty one is a mistake rather "
                "than a no-op"
            )
        program = command[0]
        # Only what the system can actually establish is checked: that something
        # runnable answers to this name. What the program then reaches for is not
        # declarable here, which is why nothing pretends to declare it.
        if shutil.which(program) is None:
            raise ValueError(
                f"external.command[0] '{program}' is neither a path to an existing "
                f"executable nor findable on PATH — the command must exist when the "
                f"stage is saved, so a name nothing resolves is refused here instead "
                f"of failing mid-run"
            )
        return command


class ExternalStage(StageBase):
    type: Literal[StageType.external]
    external: ExternalConfig
    # Exactly one input, same grain as python_row_function: the command is spawned
    # once per row of one frame, so a second input is a join or a frame function.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"external": self.external}
