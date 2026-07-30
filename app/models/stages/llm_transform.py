"""llm_transform stage: the llm handle, the strictly-1:1 schema contract (its
output must be its input plus at least one added column, under the same
primary_key), and the prompt-wiring checks — every `{placeholder}` the template
interpolates must resolve against the input edge, and a real input column must
not be double-braced (str.format_map would render it as a literal)."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Optional

from pydantic import AliasChoices, Field

from app.core.llm.options import LLMModel
from app.core.prompt_template import find_template_fields
from app.models.schema import _Base
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns

if TYPE_CHECKING:
    from app.models.schema import TableSchema
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
    # dict[str, Any]: a rubric is author-shaped JSON displayed as-is, with no
    # fixed key set for a model to declare.
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
    """Every `{placeholder}` the prompt template actually interpolates (per
    `find_template_fields` — the same parser the runtime renders with) that is
    absent from the resolved single input."""
    llm = stage.llm
    assert llm is not None  # Stage._handle_for_type guarantees this for type="llm_transform"
    cols = resolve_input_columns(stage, 0)
    return [
        COLUMN_ISSUE.format(sid=stage.id, field=f"llm prompt {{{field}}}", col=field, cols=sorted(cols))
        for field in sorted(find_template_fields(llm.prompt_data_template))
        if field not in cols
    ]


def find_llm_one_to_one_issues(
    input_schema: "TableSchema", output_schema: "TableSchema"
) -> list[str]:
    """Every way the declared schemas break the 1:1 contract: a missing or
    disagreeing primary_key, an input column the output does not carry through
    unchanged, or an output that adds no column."""
    issues = _find_primary_key_issues(input_schema, output_schema)
    issues.extend(_find_additive_shape_issues(input_schema, output_schema))
    return issues


def find_llm_double_braced_column_issues(
    input_schema: "TableSchema", template: str
) -> list[str]:
    """One issue naming every real input column the template escapes as
    `{{col}}` instead of injecting — str.format_map renders that as the literal
    text `{col}`, so the row's value silently never reaches the model."""
    injected = find_template_fields(template)
    double_braced = sorted(
        column.name for column in input_schema.columns
        if column.name not in injected
        and re.search(r"\{\{\s*" + re.escape(column.name) + r"\s*\}\}", template)
    )
    if not double_braced:
        return []
    return [
        f"llm_transform prompt_data_template double-braces input column(s) "
        f"{double_braced}: str.format_map treats double braces as an escaped "
        f"literal and never injects the value. Use single braces around the "
        f"column name."
    ]


def _find_primary_key_issues(
    input_schema: "TableSchema", output_schema: "TableSchema"
) -> list[str]:
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


def _find_additive_shape_issues(
    input_schema: "TableSchema", output_schema: "TableSchema"
) -> list[str]:
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
