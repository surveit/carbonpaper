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
    """Every compiler warning for one workflow, errors first."""

    warnings: list[CompilerWarning]

    @property
    def errors(self) -> list[CompilerWarning]:
        return [w for w in self.warnings if w.severity is UserFacingErrorSeverity.error]

    @property
    def is_clean(self) -> bool:
        """True when no error remains; a warning still owes the reviewer a sentence."""
        return not self.errors


def find_workflow_compiler_warnings(
    stages: list[Stage], failing_examples: Mapping[str, int] | None = None
) -> CompilerWarningReport:
    # `failing_examples` is {stage id: how many of its examples do not pass}. The
    # CALLER runs them: answering it means executing code, and app.runtime imports
    # this module, so running them here would be a cycle.
    """Every compiler warning across `stages`, errors first then by kind."""
    failing = failing_examples or {}
    warnings = [w for stage in stages
                for w in find_stage_compiler_warnings(stage, failing.get(stage.id))]
    warnings += _find_stale_input_schema_warnings(stages)
    order = list(SEVERITY)
    return CompilerWarningReport(
        warnings=sorted(warnings,
                        key=lambda w: (w.severity is not UserFacingErrorSeverity.error, order.index(w.kind)))
    )


def _find_stale_input_schema_warnings(stages: list[Stage]) -> list[CompilerWarning]:
    """Every input schema naming fewer columns than its upstream now produces."""
    by_id = {stage.id: stage for stage in stages}
    # An input `schema` CACHES what the upstream promises at this position; it
    # does not narrow it (that is `signature.reads`, checked separately). So an
    # omitted column is drift, and it does not stay local: an `extends` output is
    # this schema extended, so the column silently leaves the declared output of
    # every stage downstream while still flowing through them at runtime.
    warnings: list[CompilerWarning] = []
    for stage in stages:
        for ref in stage.inputs:
            upstream = by_id.get(ref.id)
            produced = upstream.resolve_output_schema() if upstream else None
            if produced is None:
                continue  # a dangling input or a publish upstream: not this check's story
            declared = {column.name for column in ref.table_schema.columns}
            missing = [c.name for c in produced.columns if c.name not in declared]
            if missing:
                warnings.append(warn(
                    stage, "stale_input_schema",
                    f"its schema for input `{ref.id}` is missing {missing}, which "
                    f"`{ref.id}` now produces — re-read the upstream's output",
                ))
    return warnings


def find_stage_compiler_warnings(
    stage: Stage, failing_examples: int | None = None
) -> list[CompilerWarning]:
    """Every warning for `stage` alone; only `examples_failing` needs them run."""
    warnings = stage.find_handle_compiler_warnings()
    # A stage with no description has nothing for examples to check, so complaining
    # about the examples too would be noise — fix the description first.
    if not any(w.kind == "undescribed" for w in warnings):
        warnings += _find_unchecked_description_warnings(stage, failing_examples)
    return warnings + _find_deliberate_choice_warnings(stage)


def _find_unchecked_description_warnings(
    stage: Stage, failing_examples: int | None
) -> list[CompilerWarning]:
    """A description nothing checks against the code; only authored code has one."""
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
    """The one warning that needs the examples RUN, reported here with the rest."""
    if not failing_examples:
        return []
    total = len(stage.tests or [])
    return [warn(stage, "examples_failing",
                 f"{failing_examples} of its {total} examples do not pass, so its "
                 f"description and its code disagree — one of them is wrong")]


def _find_deliberate_choice_warnings(stage: Stage) -> list[CompilerWarning]:
    """Legitimate settings that change what a reviewer sees and are invisible everywhere else."""
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
