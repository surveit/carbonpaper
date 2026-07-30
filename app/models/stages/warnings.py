"""The CompilerWarning type, and which kinds a stage edit can actually clear.

Sits below the per-handle modules that RAISE warnings (code.py, filter_rows.py) so
they can import it without depending on the collector that gathers them.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from app.models.schema import _Base

if TYPE_CHECKING:
    from app.models.stage import Stage

WarningKind = Literal[
    "undescribed",
    "unexemplified",
    "unreviewable_code",
    "untestable",
    "nondeterministic",
    "row_limit",
]

# Whether editing the stage can clear a kind, and the order the list is read in
# (fixable first).
#
# `untestable` cannot be cleared on the stage at all — a filter_rows carries a
# description no example can ever check — so treating it as fixable would leave the
# authoring agent no way to finish. The other two are deliberate authoring choices:
# wrong to refuse, still worth telling a reviewer about.
FIXABLE: dict[str, bool] = {
    "undescribed": True,
    "unexemplified": True,
    "unreviewable_code": True,
    "untestable": False,
    "nondeterministic": False,
    "row_limit": False,
}


class CompilerWarning(_Base):
    """One thing wrong with one stage as written."""

    kind: WarningKind
    stage_id: str
    stage_name: str
    detail: str

    @property
    def blocking(self) -> bool:
        """Can editing the stage clear this? A non-blocking warning is not thereby
        unimportant — it still owes a reviewer a sentence rather than silence — it
        just cannot be fixed by editing the stage."""
        return FIXABLE[self.kind]


def warn(stage: "Stage", kind: WarningKind, detail: str) -> CompilerWarning:
    """A warning about `stage`, named for the reader rather than for the field."""
    return CompilerWarning(
        kind=kind, stage_id=stage.id, stage_name=stage.name, detail=detail
    )
