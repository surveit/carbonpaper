"""llm_transform stage: the handle config, the prompt-wiring checks (every
`{placeholder}` resolves against the input edge, and no input column is
double-braced), and the 1:1 additive contract its declared schemas must meet."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Optional

from pydantic import AliasChoices, Field

from app.core.llm.options import LLMModel
from app.core.prompt_template import find_template_fields
from app.models.schema import TableSchema, _Base
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns

if TYPE_CHECKING:
    from app.models.stage import Stage


class LLMConfig(_Base):
    """llm_transform handle."""
    # Every field changes what this stage computes (the prompt, the model, the
    # sampling/response knobs) — see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "prompt_instructions", "prompt_data_template", "model", "temperature",
        "max_retries", "response_format", "rubric", "tools", "batch_size",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    prompt_instructions: str = ""
    prompt_data_template: str = Field(
        validation_alias=AliasChoices("prompt_data_template", "prompt_template"),
        description=(
            "Sent to the model once per input row, rendered with Python's "
            "str.format_map over the row — inject a column as {column_name}. "
            "Row-invariant guidance belongs in prompt_instructions."
        ),
    )
    model: Optional[LLMModel] = None
    temperature: float = 0.0
    max_retries: int = 3
    response_format: Literal["json", "text"] = "json"
    rubric: Optional[dict[str, Any]] = None
    tools: Optional[list[str]] = None
    batch_size: int = Field(
        default=1,
        ge=1,
        description=(
            "Rows per model call (default 1). >1 amortizes the prompt_instructions "
            "prefix across rows, which matters when that prefix is large; the runtime "
            "still returns exactly one row out per row in. But batch-mates share one "
            "context, so a row's answer can be influenced by them — keep 1 when each "
            "row needs an independent judgment."
        ),
    )


def find_llm_prompt_column_issues(stage: "Stage") -> list[str]:
    """Every way the prompt template's column references are wrong: a
    `{placeholder}` absent from the resolved input, or an input column that is
    double-braced and so never injected."""
    llm = stage.llm
    assert llm is not None  # Stage._handle_for_type guarantees this for type="llm_transform"
    cols = resolve_input_columns(stage, 0)
    injected = find_template_fields(llm.prompt_data_template)
    issues = [
        COLUMN_ISSUE.format(sid=stage.id, field=f"llm prompt {{{field}}}", col=field, cols=sorted(cols))
        for field in sorted(injected)
        if field not in cols
    ]
    issues.extend(
        find_double_braced_input_issues(llm.prompt_data_template, injected, stage.inputs[0].table_schema)
    )
    return issues


def find_double_braced_input_issues(
    template: str, injected: set[str], input_schema: TableSchema
) -> list[str]:
    """`{{col}}` is an escaped literal under str.format_map — it renders as the
    text `{col}` and the row's value never reaches the model. Double-bracing a
    REAL input column is therefore always a mistake: the author meant to inject
    it. Reject exactly that. A prompt that injects nothing is unusual but
    allowed, so this requires no injection — only that a named input column is
    not escaped. Independent of the 1:1 grain contract: this is prompt wiring,
    not schema shape."""
    double_braced = [
        column.name for column in input_schema.columns
        if column.name not in injected
        and re.search(r"\{\{\s*" + re.escape(column.name) + r"\s*\}\}", template)
    ]
    if not double_braced:
        return []
    return [
        f"llm_transform prompt_data_template double-braces input column(s) "
        f"{sorted(double_braced)}: str.format_map treats double braces as an "
        f"escaped literal and never injects the value. Use single braces "
        f"around the column name."
    ]


def find_llm_one_to_one_issues(stage: "Stage") -> list[str]:
    """An llm_transform maps one input row to one output row, so on its
    DECLARED schemas alone it must: take exactly one input; declare a
    primary_key on both that input's schema and its output_schema, naming
    the same columns; keep every input column unchanged (a transform never
    rewrites an existing column's schema); and add at least one new column.

    Checked so the reply spec the runtime derives
    (`output_schema.subtract(input_schema)`) is exactly the added columns and
    can never throw mid-run."""
    if len(stage.inputs) != 1:
        return [f"llm_transform must have exactly one input, has {len(stage.inputs)}"]
    input_schema = stage.inputs[0].table_schema
    output_schema = stage.output_schema
    assert output_schema is not None  # Stage._schemas_declared guarantees this off publish
    issues = _find_primary_key_issues(input_schema, output_schema)
    issues.extend(_find_additive_shape_issues(input_schema, output_schema))
    return issues


# Helpers for find_llm_one_to_one_issues: it has already confirmed
# `input_schema`/`output_schema` are both declared before calling these.


def _find_primary_key_issues(input_schema: TableSchema, output_schema: TableSchema) -> list[str]:
    issues: list[str] = []
    input_pk, output_pk = input_schema.primary_key, output_schema.primary_key
    if not input_pk:
        issues.append("input schema declares no primary_key")
    if not output_pk:
        issues.append("output_schema declares no primary_key")
    if input_pk and output_pk and set(input_pk) != set(output_pk):
        issues.append(
            f"input primary_key {input_pk} != output primary_key {output_pk}"
        )
    return issues


def _find_additive_shape_issues(input_schema: TableSchema, output_schema: TableSchema) -> list[str]:
    issues: list[str] = []
    if not input_schema.is_subset_of(output_schema):
        issues.append(
            "output must keep every input column unchanged (a transform is "
            f"additive: output ⊇ input); input columns "
            f"{[c.name for c in input_schema.columns]} vs output columns "
            f"{[c.name for c in output_schema.columns]}"
        )
    input_names = {c.name for c in input_schema.columns}
    if not any(c.name not in input_names for c in output_schema.columns):
        issues.append("output_schema adds no columns beyond the input")
    return issues
