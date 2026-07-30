"""Compiler warnings: what is wrong with a workflow as WRITTEN, judged without
running anything. The authoring agent clears these (or justifies each one it leaves)
before asking a human to sign off, and the Workflow page shows the same list.
Deliberately execution-free — see docs/architecture.md.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.models.schema import FunctionKind, _Base
from app.models.stage import Stage
from app.models.stages.stage_tests import STAGE_TEST_TYPES

WarningKind = Literal[
    "undescribed",
    "unexemplified",
    "unreviewable_code",
    "untestable",
    "nondeterministic",
    "row_limit",
]

# Whether a kind is FIXABLE by editing the stage, and the order the list is read in
# (fixable first).
#
# `untestable` cannot be cleared on the stage at all — a filter_rows carries a
# description no example can ever check — so treating it as fixable would leave the
# agent no way to finish. The other two are deliberate authoring choices: wrong to
# refuse, still worth telling a reviewer about.
_BLOCKING: dict[str, bool] = {
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
        """Can the authoring agent actually clear this one? A non-blocking warning is
        not thereby unimportant — it still has to be explained to a reviewer rather
        than left silent — it just cannot be fixed by editing the stage."""
        return _BLOCKING[self.kind]


class CompilerWarningReport(BaseModel):
    """Every compiler warning for one workflow, blocking first."""

    warnings: list[CompilerWarning]

    @property
    def blocking(self) -> list[CompilerWarning]:
        return [w for w in self.warnings if w.blocking]

    @property
    def is_clean(self) -> bool:
        """True when nothing fixable remains. NOT a licence to ask for signoff with
        the rest unmentioned: a non-blocking warning still owes the reviewer a
        sentence saying why it is safe to ignore here."""
        return not self.blocking


def find_workflow_compiler_warnings(stages: list[Stage]) -> CompilerWarningReport:
    """Every compiler warning across `stages`, blocking first then by stage order."""
    warnings = [w for stage in stages for w in find_stage_compiler_warnings(stage)]
    order = list(_BLOCKING)
    return CompilerWarningReport(
        warnings=sorted(warnings, key=lambda w: (not w.blocking, order.index(w.kind)))
    )


def find_stage_compiler_warnings(stage: Stage) -> list[CompilerWarning]:
    """Every compiler warning for `stage` alone — the stage introspecting itself.

    Judged on the stage as written: no code is run, no examples are executed, no
    project on disk is read. Whether a stage's examples PASS is a different
    question, answered by running them, and is not a compiler warning."""
    return [
        *_find_description_warnings(stage),
        *_find_unreviewable_code_warnings(stage),
        *_find_deliberate_choice_warnings(stage),
    ]


def _find_description_warnings(stage: Stage) -> list[CompilerWarning]:
    """The prose a reviewer reads instead of the code, and whether anything checks
    it. Only for a stage whose behaviour IS authored code — a join's keys are
    config a reviewer reads directly, with no description standing in for them."""
    handle = stage.function or stage.filter
    if handle is None:
        return []
    if not (handle.summary or "").strip():
        return [_warn(stage, "undescribed",
                      "no plain-language description — reviewable only by reading its code")]
    if stage.type not in STAGE_TEST_TYPES:
        return [_warn(stage, "untestable",
                      f"a {stage.type} cannot carry examples, so nothing can check its "
                      f"description against its code")]
    if not stage.tests:
        return [_warn(stage, "unexemplified",
                      "has a description but no examples, so nothing checks it against "
                      "the code")]
    return []


def _find_unreviewable_code_warnings(stage: Stage) -> list[CompilerWarning]:
    """Code the panel cannot show is code nobody reviews. A `module` handle points
    at a file on disk, and the stage's fingerprint covers that PATH rather than its
    contents — so the code can change under a description that still looks
    certified."""
    if stage.function is None or stage.function.kind != FunctionKind.module:
        return []
    return [_warn(stage, "unreviewable_code",
                  f"the code lives in module `{stage.function.module}` rather than on the "
                  f"stage, so the review panel cannot show it")]


def _find_deliberate_choice_warnings(stage: Stage) -> list[CompilerWarning]:
    """Settings that are legitimate but change what a reviewer is looking at, and
    are invisible everywhere else."""
    warnings = []
    if not stage.cache:
        warnings.append(_warn(stage, "nondeterministic",
                              "declared intentionally non-deterministic (cache off), so it "
                              "re-rolls every run and its examples cannot pin its output"))
    if stage.limit is not None:
        warnings.append(_warn(stage, "row_limit",
                              f"reads at most {stage.limit} input rows, so a run over this "
                              f"stage is a sample rather than the whole input"))
    return warnings


def _warn(stage: Stage, kind: WarningKind, detail: str) -> CompilerWarning:
    return CompilerWarning(
        kind=kind, stage_id=stage.id, stage_name=stage.name, detail=detail
    )
