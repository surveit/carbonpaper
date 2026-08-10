"""The CompilerWarning type, and the severity each kind carries.

Sits below the per-handle modules that RAISE warnings (code.py, filter_rows.py) so
they can import it without depending on the collector that gathers them.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from app.models.severity import UserFacingErrorSeverity
from app.models.schema import _Base

if TYPE_CHECKING:
    from app.models.stages.stage_base import StageBase

WarningKind = Literal[
    "undescribed",
    "unexemplified",
    "examples_failing",
    "unreviewable_code",
    "nondeterministic",
    "row_limit",
]

# Each kind's severity, and the order the list is read in (errors first).
#
# `error` = an edit to this stage clears it, so it is owed one before anyone signs
# the workflow off. `examples_failing` is the one error that cannot be judged from
# the stage alone — running the examples is what answers it — so the caller runs
# them and hands the result in; either the code or the description is wrong, and
# both are edits to this stage.
#
# The last two are deliberate authoring choices, wrong to refuse and still worth
# telling a reviewer about: a `warning` is not thereby unimportant.
SEVERITY: dict[str, UserFacingErrorSeverity] = {
    "undescribed": UserFacingErrorSeverity.error,
    "unexemplified": UserFacingErrorSeverity.error,
    "examples_failing": UserFacingErrorSeverity.error,
    "unreviewable_code": UserFacingErrorSeverity.error,
    "nondeterministic": UserFacingErrorSeverity.warning,
    "row_limit": UserFacingErrorSeverity.warning,
}


class CompilerWarning(_Base):
    kind: WarningKind
    stage_id: str
    detail: str

    @property
    def severity(self) -> UserFacingErrorSeverity:
        return SEVERITY[self.kind]


def warn(stage: "StageBase", kind: WarningKind, detail: str) -> CompilerWarning:
    return CompilerWarning(kind=kind, stage_id=stage.id, detail=detail)
