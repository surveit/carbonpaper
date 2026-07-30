"""Compiler warnings: what is wrong with a workflow as WRITTEN, judged without
running anything. Collected here; RAISED by the module that owns each config block.
The authoring agent clears these (or justifies each one it leaves) before asking a
human to sign off, and the Workflow page shows the same list.
"""
from __future__ import annotations

from pydantic import BaseModel

from app.models.stage import Stage
from app.models.stages.stage_tests import STAGE_TEST_TYPES
from app.models.stages.warnings import FIXABLE, CompilerWarning, warn


class CompilerWarningReport(BaseModel):
    """Every compiler warning for one workflow, fixable first."""

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
    """Every compiler warning across `stages`, fixable first then by kind."""
    warnings = [w for stage in stages for w in find_stage_compiler_warnings(stage)]
    order = list(FIXABLE)
    return CompilerWarningReport(
        warnings=sorted(warnings, key=lambda w: (not w.blocking, order.index(w.kind)))
    )


def find_stage_compiler_warnings(stage: Stage) -> list[CompilerWarning]:
    """Every compiler warning for `stage` alone.

    Judged on the stage as written: no code is run, no examples are executed, no
    project on disk is read. Whether a stage's examples PASS is a different question,
    answered by running them, and is not a compiler warning."""
    warnings = stage.find_handle_compiler_warnings()
    # A stage with no description has nothing for examples to check, so complaining
    # about the examples too would be noise — fix the description first.
    if not any(w.kind == "undescribed" for w in warnings):
        warnings += _find_unchecked_description_warnings(stage)
    return warnings + _find_deliberate_choice_warnings(stage)


def _find_unchecked_description_warnings(stage: Stage) -> list[CompilerWarning]:
    """A description nothing checks against the code — asked only of a stage that
    HAS authored code, since a config-only stage has no description to check."""
    if stage.find_authored_code_block() is None:
        return []
    if stage.type not in STAGE_TEST_TYPES:
        return [warn(stage, "untestable",
                     f"a {stage.type} cannot carry examples, so nothing can check its "
                     f"description against its code")]
    if not stage.tests:
        return [warn(stage, "unexemplified",
                     "has a description but no examples, so nothing checks it against "
                     "the code")]
    return []


def _find_deliberate_choice_warnings(stage: Stage) -> list[CompilerWarning]:
    """Settings that are legitimate but change what a reviewer is looking at, and are
    invisible everywhere else. Stage's own fields, no config block involved."""
    warnings = []
    if not stage.cache:
        warnings.append(warn(stage, "nondeterministic",
                             "declared intentionally non-deterministic (cache off), so it "
                             "re-rolls every run and its examples cannot pin its output"))
    if stage.limit is not None:
        warnings.append(warn(stage, "row_limit",
                             f"reads at most {stage.limit} input rows, so a run over this "
                             f"stage is a sample rather than the whole input"))
    return warnings
