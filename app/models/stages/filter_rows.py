"""filter_rows stage: the config block, plus the output-side check — it keeps a
subset of its single input's rows unchanged, so a declared output_schema must
equal the input schema."""
from __future__ import annotations

from typing import ClassVar, Literal, Optional

from pydantic import Field, model_validator

from app.models.schema import StageConfig
from app.models.stage_base import StageBase, StageInput, StageType
from app.models.stages.code import (
    CORNER_CASES_DESCRIPTION,
    SUMMARY_DESCRIPTION,
    CornerCase,
    validate_inline_function_code,
)


class FilterConfig(StageConfig):
    """filter_rows config block: an authored row predicate, `def should_include(row:
    dict) -> bool`. True keeps the row, False drops it; every kept row's
    columns pass through unchanged and its relative order is preserved.

    Inline code is the only source for that predicate: a filter decides, and a
    decision that needs an importable module is doing more than deciding. There
    is deliberately no `kind`/`module` here, unlike PythonFunction."""
    # Every field changes what this stage computes (the predicate it runs)
    # except `summary`, which describes that predicate to a reader — see
    # StageBase.compute_definition_fingerprint.
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
            "row. Returning anything other than a bool is an error."
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
    filter: FilterConfig
    # Exactly one input: a predicate decides row by row, and two inputs is a
    # join or a python_frame_function.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"filter": self.filter}

    def find_output_schema_issues(self) -> list[str]:
        return find_filter_output_issues(self)

    def find_authored_code_block(self) -> FilterConfig:
        return self.filter


def find_filter_output_issues(stage: "FilterRowsStage") -> list[str]:
    """Issue naming any column where the declared output_schema disagrees with
    the single input's schema."""
    assert stage.output_schema is not None  # StageBase._schemas_declared guarantees this
    input_schema = stage.inputs[0].table_schema
    differing = sorted(stage.output_schema.differing_column_names(input_schema))
    if not differing:
        return []
    return [
        f"stage '{stage.id}': output_schema must equal its input schema; differs "
        f"on column(s) {differing}"
    ]
