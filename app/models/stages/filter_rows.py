"""filter_rows stage: the config block, plus the output-side check — it keeps a
subset of its single input's rows unchanged, so its signature declares reads
only — never adds or rewrites."""
from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, Literal, Optional

from pydantic import Field, model_validator

from app.models.errors import StepRefused
from app.models.schema import StageConfig
from app.models.stages.stage_base import StageBase, StageInput, StageType
from app.models.stages.warnings import CompilerWarning, warn
from app.models.stages.code import (
    CORNER_CASES_DESCRIPTION,
    SUMMARY_DESCRIPTION,
    CornerCase,
    validate_inline_function_code,
)
from app.models.stages.node_spec import NodeTypeSpec
from app.models.stages.signature import ExtendsSignature
from app.models.stages.stage_tests import FilterRowsStageTest


class FilterConfig(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"code", "function"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({"summary", "corner_cases"})

    summary: Optional[str] = Field(default=None, description=SUMMARY_DESCRIPTION)
    corner_cases: list[CornerCase] = Field(
        default_factory=list, description=CORNER_CASES_DESCRIPTION
    )
    code: str = Field(
        description=(
            "Inline Python defining `should_include` (or whatever `function` names). "
            "Signature: `def should_include(row: dict) -> bool` — True keeps the "
            "row. Returning anything other than a bool is an error. A row it cannot "
            "honestly decide is refused, not guessed: "
            f"`raise {StepRefused.__name__}(\"why\")` (no import needed). A guessed "
            "False silently drops a row that belonged — e.g. a blank `status`, or a "
            "code the predicate has never seen."
        ),
    )
    function: Optional[str] = Field(
        default=None,
        description=(
            "Name of the predicate to call within `code`, defaulting to "
            "`should_include`. `code` says what is defined; this says which name in "
            "it to call — set it only when the predicate is not called "
            "`should_include`."
        ),
    )

    @model_validator(mode="after")
    def _inline_code_is_runnable(self) -> "FilterConfig":
        validate_inline_function_code(
            self.code, self.function, default_name="should_include", return_hint="a bool"
        )
        return self


class FilterRowsStage(StageBase):
    type: Literal[StageType.filter_rows]
    CARRIES_RUNNABLE_TESTS: ClassVar[bool] = True
    filter: FilterConfig
    # Exactly one input: a predicate decides row by row, and two inputs is a
    # join or a python_frame_function.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    tests: Optional[Sequence[FilterRowsStageTest]] = None
    signature: ExtendsSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"filter": self.filter}

    def find_signature_config_issues(self) -> list[str]:
        signature = self.signature
        assert signature is not None  # find_signature_config_issues runs only with one
        if signature.adds or signature.rewrites:
            return [
                f"stage '{self.id}': filter_rows keeps every kept row's columns "
                f"unchanged — its signature declares reads only, never adds or rewrites"
            ]
        # An empty read set decides every row on an empty row: all kept or all dropped.
        if not signature.reads:
            return [
                f"stage '{self.id}': its signature reads nothing, so the predicate would "
                f"be handed an empty row and decide every row the same way — declare the "
                f"columns `{self.filter.function or 'should_include'}` consumes"
            ]
        return []

    def find_authored_code_block(self) -> FilterConfig:
        return self.filter

    def find_handle_compiler_warnings(self) -> list[CompilerWarning]:
        return find_filter_warnings(self)


def find_filter_warnings(stage: "FilterRowsStage") -> list[CompilerWarning]:
    if not (stage.filter.summary or "").strip():
        return [warn(stage, "undescribed",
                     "no plain-language description — reviewable only by reading its code")]
    return []

# Authoring copy for this module's stage type(s); assembled into NODE_TYPES.
NODE_TYPE_SPECS: dict[str, NodeTypeSpec] = {
    "filter_rows": NodeTypeSpec(
        summary="Keep the rows an authored predicate returns True for.",
        signature_form="extends",
        blocks=["filter"],
        requires_inputs=True,
        min_inputs=1,
        required=["code"],
        optional=["function", "summary"],
        notes=(
            "Takes exactly ONE input. The predicate is INLINE code only — there is no "
            "kind/module here; a filter that needs an importable module is doing more "
            "than deciding. `should_include(row)` is handed a plain dict and "
            "must return a bool — True keeps the row, False drops it; any other return "
            "type is a run-time error. Kept rows preserve their original relative order "
            "and every column unchanged, so the signature never adds or rewrites. The "
            "predicate is handed exactly the columns the signature `reads`, so those must "
            "cover every column it consumes and may never be empty."
        ),
    ),
}
