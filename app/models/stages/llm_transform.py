"""Config-column validation for an llm_transform stage: every `{placeholder}`
its prompt template actually interpolates must resolve against the stage's
input edge."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.prompt_template import find_template_fields
from app.models.schema import TableSchema
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns

if TYPE_CHECKING:
    from app.models.stage import Stage


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


# ── llm_transform's 1:1 contract ─────────────────────────────────────────────
# Helpers for Stage._llm_transform_one_to_one: it has already confirmed
# `input_schema`/`output_schema` are both declared before calling these.


def find_primary_key_issues(input_schema: TableSchema, output_schema: TableSchema) -> list[str]:
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


def find_additive_shape_issues(input_schema: TableSchema, output_schema: TableSchema) -> list[str]:
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
