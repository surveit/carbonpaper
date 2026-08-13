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
    "nondeterministic",
]

# The order the list is read in. Every kind is a `warning`: nothing here refuses an
# action, a version snapshots whatever the author has, and each of these is something
# an author may knowingly leave standing — a stage described in code alone, one no
# example checks, a model call that re-rolls every run. `error` is the RUNTIME's
# word, for a stage that actually stopped (app/web/run_issues.py), and a compiler
# note borrowing it claimed a severity it could not act on.
SEVERITY: dict[str, UserFacingErrorSeverity] = {
    "undescribed": UserFacingErrorSeverity.warning,
    "unexemplified": UserFacingErrorSeverity.warning,
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
