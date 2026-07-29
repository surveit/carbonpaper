"""filter_rows stage: handle config, plus the output-side check — it keeps a
subset of its single input's rows unchanged, so a declared output_schema must
equal the input schema."""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Optional

from pydantic import Field, model_validator

from app.models.schema import FunctionKind, _Base
from app.models.stages.code import validate_inline_function_code

if TYPE_CHECKING:
    from app.models.stage import Stage


class FilterConfig(_Base):
    """filter_rows handle: an authored row predicate, `def should_include(row:
    dict) -> bool`. True keeps the row, False drops it; every kept row's
    columns pass through unchanged and its relative order is preserved."""
    # Every field changes what this stage computes (the predicate code/module
    # it runs) — see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "kind", "code", "module", "function", "requirements",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    kind: FunctionKind
    code: Optional[str] = Field(
        default=None,
        description=(
            "Inline Python defining `should_include` (default `should_include`). "
            "Signature: `def should_include(row: dict) -> bool` — True keeps the "
            "row. Returning anything other than a bool is an error."
        ),
    )
    module: Optional[str] = None
    function: Optional[str] = None
    requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kind_fields(self) -> "FilterConfig":
        if self.kind == FunctionKind.module and not self.module:
            raise ValueError("filter.kind=module needs `module`")
        if self.kind == FunctionKind.inline and not self.code:
            raise ValueError("filter.kind=inline needs `code`")
        return self

    @model_validator(mode="after")
    def _inline_code_is_runnable(self) -> "FilterConfig":
        if self.kind != FunctionKind.inline or not self.code:
            return self
        validate_inline_function_code(
            self.code, self.function, default_name="should_include", return_hint="a bool"
        )
        return self


def find_filter_output_issues(stage: "Stage") -> list[str]:
    """Issue naming any column where the declared output_schema disagrees with
    the single input's schema."""
    assert stage.output_schema is not None  # Stage._schemas_declared guarantees this off publish
    input_schema = stage.inputs[0].table_schema
    differing = sorted(stage.output_schema.differing_column_names(input_schema))
    if not differing:
        return []
    return [
        f"stage '{stage.id}': output_schema must equal its input schema; differs "
        f"on column(s) {differing}"
    ]
