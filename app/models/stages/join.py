"""Column validation for a merge stage (`enrich`/`expand`), on both the input
and output side: every key's `.subject`/`.reference` must resolve against its
side's stage input edge; and a declared output_schema (plus `select`) must be
deliverable by the columns the merge actually produces. Both types validate
identically — they differ only in the cardinality the RUNTIME enforces."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.stages.shared import (
    COLUMN_ISSUE,
    find_declared_vs_derived_issues,
    resolve_input_columns,
)

if TYPE_CHECKING:
    from app.models.schema import TableSchema
    from app.models.stage import JoinConfig, Stage

SELECT_UNPRODUCIBLE_ISSUE = (
    "stage '{sid}': join.select references column '{col}' that the merge "
    "cannot produce (producible columns: {cols})"
)


def find_join_column_issues(stage: "Stage") -> list[str]:
    """Every merge key whose `.subject`/`.reference` names a column absent from
    its resolved side's input."""
    merge = stage.join
    assert merge is not None  # Stage._handle_for_type guarantees this for the merge types
    subject = resolve_input_columns(stage, 0)
    reference = resolve_input_columns(stage, 1)
    issues: list[str] = []
    for key in merge.keys:
        if key.subject not in subject:
            issues.append(
                COLUMN_ISSUE.format(sid=stage.id, field="join key .subject", col=key.subject,
                                    cols=sorted(subject))
            )
        if key.reference not in reference:
            issues.append(
                COLUMN_ISSUE.format(sid=stage.id, field="join key .reference", col=key.reference,
                                    cols=sorted(reference))
            )
    return issues


def find_join_output_issues(stage: "Stage") -> list[str]:
    """Every declared output_schema column (and select entry) the merge handle
    cannot deliver."""
    merge = stage.join
    assert merge is not None  # Stage._handle_for_type guarantees this for the merge types
    assert stage.output_schema is not None  # Stage._schemas_declared guarantees this off publish
    subject = stage.inputs[0].table_schema
    reference = stage.inputs[1].table_schema
    merged = derive_join_output_types(merge, subject, reference)
    issues = [
        SELECT_UNPRODUCIBLE_ISSUE.format(sid=stage.id, col=entry, cols=sorted(merged))
        for entry in merge.select or []
        if entry not in merged
    ]
    effective = (
        {name: merged[name] for name in merge.select if name in merged}
        if merge.select else merged
    )
    issues.extend(
        find_declared_vs_derived_issues(stage.id, "merge", stage.output_schema, effective)
    )
    return issues


def derive_join_output_types(
    merge: "JoinConfig", subject: "TableSchema", reference: "TableSchema"
) -> dict[str, str]:
    """The columns the merge emits, each mapped to its type; `select` is NOT applied here."""
    # Mirrors pandas merge(..., suffixes=("", "_r")): subject columns keep name
    # and type; a reference key sharing its subject key's name collapses into it;
    # any other reference column collides into <name>_r.
    collapsed_reference_keys = {k.reference for k in merge.keys if k.subject == k.reference}
    merged: dict[str, str] = {c.name: c.type for c in subject.columns}
    subject_names = set(merged)
    for column in reference.columns:
        if column.name in collapsed_reference_keys:
            continue
        name = column.name if column.name not in subject_names else f"{column.name}_r"
        merged[name] = column.type
    return merged
