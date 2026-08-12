"""Compiler warnings: what is wrong with a workflow as WRITTEN, judged without
running anything. Collected here; RAISED by the module that owns each config block.
The authoring agent clears these (or justifies each one it leaves) before asking a
human to sign off, and the Workflow page shows the same list.
"""
from __future__ import annotations

from typing import Mapping

from pydantic import BaseModel

from app.models.severity import UserFacingErrorSeverity
from app.models.stage import Stage
from app.models.stages.warnings import SEVERITY, CompilerWarning, warn


class CompilerWarningReport(BaseModel):
    warnings: list[CompilerWarning]

    @property
    def errors(self) -> list[CompilerWarning]:
        return [w for w in self.warnings if w.severity is UserFacingErrorSeverity.error]

    @property
    def is_clean(self) -> bool:
        return not self.errors


def find_workflow_compiler_warnings(
    stages: list[Stage], failing_examples: Mapping[str, int] | None = None
) -> CompilerWarningReport:
    """The CALLER runs the examples: app.runtime imports this module, so running them here cycles."""
    failing = failing_examples or {}
    warnings = [w for stage in stages
                for w in find_stage_compiler_warnings(stage, failing.get(stage.id))]
    order = list(SEVERITY)
    return CompilerWarningReport(
        warnings=sorted(warnings,
                        key=lambda w: (w.severity is not UserFacingErrorSeverity.error, order.index(w.kind)))
    )


def find_stage_compiler_warnings(
    stage: Stage, failing_examples: int | None = None
) -> list[CompilerWarning]:
    warnings = stage.find_handle_compiler_warnings()
    # A stage with no description has nothing for examples to check, so complaining
    # about the examples too would be noise — fix the description first.
    if not any(w.kind == "undescribed" for w in warnings):
        warnings += _find_unchecked_description_warnings(stage, failing_examples)
    return warnings + _find_deliberate_choice_warnings(stage)


def _find_unchecked_description_warnings(
    stage: Stage, failing_examples: int | None
) -> list[CompilerWarning]:
    if stage.find_authored_code_block() is None:
        return []
    if not stage.CARRIES_RUNNABLE_TESTS:
        # No example can ever run here, so asking for one would never clear.
        return []
    if not stage.tests:
        return [warn(stage, "unexemplified",
                     "has a description but no examples, so nothing checks it against "
                     "the code")]
    return _find_failing_example_warning(stage, failing_examples)


def _find_failing_example_warning(
    stage: Stage, failing_examples: int | None
) -> list[CompilerWarning]:
    if not failing_examples:
        return []
    total = len(stage.tests or [])
    return [warn(stage, "examples_failing",
                 f"{failing_examples} of its {total} examples "
                 f"{'mismatches' if failing_examples == 1 else 'mismatch'} what an "
                 f"independent AI agent expected. Further review recommended")]


def _find_deliberate_choice_warnings(stage: Stage) -> list[CompilerWarning]:
    warnings = []
    if not stage.cache:
        warnings.append(warn(stage, "nondeterministic",
                             "declared intentionally non-deterministic (cache off), so it "
                             "re-rolls every run and its examples cannot pin its output"))
    return warnings
