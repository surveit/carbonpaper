"""The CompilerWarning type, and the severity each kind carries.

Sits below the per-handle modules that RAISE warnings (code.py, filter_rows.py) so
they can import it without depending on the collector that gathers them.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from app.models.severity import UserFacingErrorSeverity
from app.models.schema import _Base

if TYPE_CHECKING:
    from app.models.stages.stage_base import AbstractStage

WarningKind = Literal[
    "undescribed",
    "unexemplified",
    "examples_failing",
    "unreviewable_code",
    "nondeterministic",
]

# Each kind's severity, and the order the list is read in (errors first).
#
# `error` = an edit to this stage clears it, so it is owed one before anyone signs
# the workflow off.
#
# `examples_failing` is a warning: it reports that an agent reading only the
# description predicted something the code did not do. The agent may simply have
# read the description a different way, so a human deciding the code is right
# resolves it with no edit owed. `nondeterministic` is a deliberate authoring
# choice. Both are wrong to refuse and still worth telling a reviewer about: a
# `warning` is not thereby unimportant.
SEVERITY: dict[str, UserFacingErrorSeverity] = {
    "undescribed": UserFacingErrorSeverity.error,
    "unexemplified": UserFacingErrorSeverity.error,
    "unreviewable_code": UserFacingErrorSeverity.error,
    "examples_failing": UserFacingErrorSeverity.warning,
    "nondeterministic": UserFacingErrorSeverity.warning,
}


class CompilerWarning(_Base):
    kind: WarningKind
    stage_id: str
    detail: str

    @property
    def severity(self) -> UserFacingErrorSeverity:
        return SEVERITY[self.kind]


def warn(stage: "AbstractStage", kind: WarningKind, detail: str) -> CompilerWarning:
    return CompilerWarning(kind=kind, stage_id=stage.id, detail=detail)
