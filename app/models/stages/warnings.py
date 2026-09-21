"""The CompilerWarning type, and the severity each kind carries.

Sits below the per-handle modules that RAISE warnings (code.py, filter_rows.py) so
they can import it without depending on the collector that gathers them.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from app.models.severity import UserFacingErrorSeverity
from app.models.schema import _Base
from app.core.ids import ID

if TYPE_CHECKING:
    from app.models.stages.stage_base import AbstractStage

WarningKind = Literal[
    "undescribed",
    "unsaid_test",
    "unnamed_rows",
    "unexemplified",
    "examples_failing",
    "nondeterministic",
]

# Read in this order. Which severity a kind carries: docs/visual-language.md
SEVERITY: dict[str, UserFacingErrorSeverity] = {
    "undescribed": UserFacingErrorSeverity.warning,
    "unsaid_test": UserFacingErrorSeverity.warning,
    "unnamed_rows": UserFacingErrorSeverity.error,
    "unexemplified": UserFacingErrorSeverity.warning,
    "examples_failing": UserFacingErrorSeverity.warning,
    "nondeterministic": UserFacingErrorSeverity.warning,
}


class CompilerWarning(_Base):
    kind: WarningKind
    stage_id: ID
    detail: str

    @property
    def severity(self) -> UserFacingErrorSeverity:
        return SEVERITY[self.kind]


def warn(stage: "AbstractStage", kind: WarningKind, detail: str) -> CompilerWarning:
    return CompilerWarning(kind=kind, stage_id=stage.id, detail=detail)
