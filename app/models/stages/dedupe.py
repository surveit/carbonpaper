"""dedupe stage: the config block, plus validation that `keys` and the tie-breaking
`by` column resolve against the input, and that the signature writes nothing —
a survivor is an input row untouched."""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Literal, Optional, Sequence

from pydantic import Field, model_validator

from app.models.schema import StageConfig
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ExtendsSignature

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStageInput


class DedupeKeep(str, Enum):
    first = "first"  # position-dependent: means something only if an upstream stage fixed the order
    highest = "highest"
    lowest = "lowest"


class DedupeConfig(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"keys", "keep", "by"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    keys: list[str] = Field(
        min_length=1,
        description=(
            "The columns that identify one thing. Rows agreeing on all of them are "
            "duplicates of each other and collapse to a single row."
        ),
    )
    keep: DedupeKeep = Field(
        default=DedupeKeep.first,
        description=(
            "Which duplicate survives. `highest`/`lowest` pick it by `by`. `first` takes "
            "the earliest row in the frame, which is only meaningful when an upstream "
            "stage fixed the order — prefer stating the rule with `by`."
        ),
    )
    by: Optional[str] = Field(
        default=None,
        description="The column `highest`/`lowest` compare. Required for those, unused by `first`.",
    )

    @model_validator(mode="after")
    def _by_matches_keep(self) -> "DedupeConfig":
        if self.keep == DedupeKeep.first:
            if self.by is not None:
                raise ValueError("dedupe: keep=first picks by position, so it takes no `by`")
            return self
        if not self.by:
            raise ValueError(f"dedupe: keep={self.keep} needs `by` to compare rows on")
        return self


class DedupeStage(AbstractStage):
    type: Literal[StageType.dedupe]
    dedupe: DedupeConfig
    # Exactly one input: collapsing duplicates within one frame. Two is a join.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    signature: ExtendsSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"dedupe": self.dedupe}

    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_dedupe_column_issues(self, inputs)

    def find_signature_config_issues(self) -> list[str]:
        signature = self.signature
        if not (signature.adds or signature.rewrites):
            return []
        return [
            f"stage '{self.id}': dedupe keeps a subset of its input's rows unchanged — "
            f"its signature declares reads only, never adds or rewrites"
        ]


def find_dedupe_column_issues(
    stage: "DedupeStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    cols = resolve_input_columns(inputs, 0)
    dedupe = stage.dedupe
    issues = [
        COLUMN_ISSUE.format(sid=stage.id, field="dedupe.keys", col=key, cols=sorted(cols))
        for key in dedupe.keys
        if key not in cols
    ]
    if dedupe.by and dedupe.by not in cols:
        issues.append(COLUMN_ISSUE.format(
            sid=stage.id, field="dedupe.by", col=dedupe.by, cols=sorted(cols)
        ))
    return issues


# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "dedupe": StageTypeSpec(
        summary="Collapse rows that agree on `keys` to one, choosing the survivor by a stated rule.",
        signature_form="extends",
        blocks=["dedupe"],
        requires_inputs=True,
        min_inputs=1,
        required=["keys"],
        optional=["keep", "by"],
        notes=(
            "Takes exactly ONE input, and never alters a row: the output is a SUBSET of "
            "the input rows, so `produces` restates the columns the workflow carries on "
            "and reads nothing.\n"
            "Say WHY one duplicate wins. `keep: highest, by: filed_at` keeps the latest "
            "filing and says so in the config, where a reviewer reads it. `keep: first` "
            "depends on the order the rows happen to arrive in, so it states nothing a "
            "reviewer can check — use it only when an upstream stage fixed that order "
            "deliberately.\n"
            "Ties on `by` are broken by position, so a `by` that repeats within a key "
            "group is a rule that has not finished deciding — add a second key or a "
            "sharper `by`.\n"
            "The runtime records the row that survived AND the rows that lost to it, so a "
            "reader tracing an output row can see what was collapsed into it."
        ),
    ),
}
