"""starlark_filter_rows stage: the sandboxed counterpart of filter_rows. Its
predicate is validated the same way a starlark_row_function's transform is —
the shared compile-and-bind check in app.models.stages.starlark."""
from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, Literal, Optional

from pydantic import Field, model_validator

from app.models.schema import StageConfig
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.code import CORNER_CASES_DESCRIPTION, SUMMARY_DESCRIPTION, CornerCase
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ExtendsSignature
from app.models.stages.stage_tests import FilterRowsStageTest
from app.models.stages.starlark import (
    STARLARK_LANGUAGE_NOTE,
    validate_starlark_function_code,
)
from app.models.stages.warnings import CompilerWarning, warn

DEFAULT_PREDICATE_NAME = "should_include"

_CODE_DESCRIPTION = (
    "Inline Starlark defining `function` (default `should_include`): "
    "`def should_include(row): ...` — one row dict in, True to keep it, False to drop "
    "it. Returning anything other than a bool stops the step. A row it cannot honestly "
    "decide is refused, not guessed: `refuse(\"reason\")` (no import needed). A guessed "
    "False silently drops a row that belonged — a blank `status`, or a code the "
    "predicate has never seen. " + STARLARK_LANGUAGE_NOTE
)


class StarlarkFilter(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"code", "function"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({"summary", "corner_cases"})

    summary: Optional[str] = Field(default=None, description=SUMMARY_DESCRIPTION)
    corner_cases: list[CornerCase] = Field(
        default_factory=list, description=CORNER_CASES_DESCRIPTION
    )
    code: str = Field(description=_CODE_DESCRIPTION)
    function: Optional[str] = Field(
        default=None,
        description=(
            "Name of the predicate to call within `code`, defaulting to "
            f"`{DEFAULT_PREDICATE_NAME}`. Set it only when the predicate is not called that."
        ),
    )

    @model_validator(mode="after")
    def _source_is_runnable(block: "StarlarkFilter") -> "StarlarkFilter":
        validate_starlark_function_code(
            block.code, block.function, default_name=DEFAULT_PREDICATE_NAME,
            return_hint="a bool",
        )
        return block


class StarlarkFilterRowsStage(AbstractStage):
    type: Literal[StageType.starlark_filter_rows]
    CARRIES_RUNNABLE_TESTS: ClassVar[bool] = True
    starlark_filter: StarlarkFilter
    # Exactly one input: a predicate decides row by row, and two inputs is a join.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    tests: Optional[Sequence[FilterRowsStageTest]] = None
    signature: ExtendsSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"starlark_filter": self.starlark_filter}

    def find_authored_code_block(self) -> StarlarkFilter:
        return self.starlark_filter

    def find_handle_compiler_warnings(self) -> list[CompilerWarning]:
        if (self.starlark_filter.summary or "").strip():
            return []
        return [warn(self, "undescribed",
                     "no plain-language description — reviewable only by reading its code")]

    def find_signature_config_issues(self) -> list[str]:
        return find_starlark_filter_signature_issues(self)


def find_starlark_filter_signature_issues(
    stage: "StarlarkFilterRowsStage"
) -> list[str]:
    signature = stage.signature
    if signature.adds or signature.rewrites:
        return [
            f"stage '{stage.id}': starlark_filter_rows keeps every kept row's columns "
            f"unchanged — its signature declares reads only, never adds or rewrites"
        ]
    # An empty read set decides every row on an empty row: all kept or all dropped.
    if not signature.reads:
        return [
            f"stage '{stage.id}': its signature reads nothing, so the predicate would be "
            f"handed an empty row and decide every row the same way — declare the columns "
            f"`{stage.starlark_filter.function or DEFAULT_PREDICATE_NAME}` consumes"
        ]
    return []

# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "starlark_filter_rows": StageTypeSpec(
        summary="Sandboxed Starlark run once per row: True keeps the row, False drops it.",
        signature_form="extends",
        blocks=["starlark_filter"],
        requires_inputs=True,
        min_inputs=1,
        required=["code"],
        optional=["function", "summary"],
        notes=(
            "Takes exactly ONE input and changes no cell — the output is a SUBSET of the "
            "input's rows. The signature READS the columns the predicate consults and "
            "writes nothing; reading nothing is refused, because a predicate handed an "
            "empty row decides every row the same way.\n"
            "The runtime does the selecting, so it knows which input rows survived without "
            "the predicate saying — a trace crosses this stage to the row it kept.\n"
            "Prefer this over a python step for anything a predicate can express: a "
            "reviewer who has read the code has read everything the step can do. "
            + STARLARK_LANGUAGE_NOTE
        ),
    ),
}
