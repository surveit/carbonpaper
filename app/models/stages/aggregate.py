"""Config-column validation for an aggregate stage: `group_by`, each
aggregation's `value_column`, and every column an aggregation's `where`
predicate references must resolve against the stage's input edge."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.stages.shared import (
    COLUMN_ISSUE,
    find_predicate_column_issues,
    resolve_input_columns,
)

if TYPE_CHECKING:
    from app.models.stage import Stage


def find_aggregate_column_issues(stage: "Stage") -> list[str]:
    """Every `group_by` entry, aggregation `value_column`, and column an
    aggregation's `where` references that is absent from the resolved single
    input; [] when that input's edge declares no schema at all."""
    aggregate = stage.aggregate
    assert aggregate is not None  # Stage._handle_for_type guarantees this for type="aggregate"
    cols = resolve_input_columns(stage, 0)
    if cols is None:
        return []
    issues = [
        COLUMN_ISSUE.format(sid=stage.id, field="aggregate.group_by", col=g, cols=sorted(cols))
        for g in aggregate.group_by
        if g not in cols
    ]
    issues.extend(
        COLUMN_ISSUE.format(
            sid=stage.id,
            field=f"aggregate.aggregations[{op.output_column}].value_column",
            col=op.value_column,
            cols=sorted(cols),
        )
        for op in aggregate.aggregations
        if op.value_column and op.value_column not in cols
    )
    for op in aggregate.aggregations:
        if op.where:
            issues.extend(find_predicate_column_issues(
                op.where, stage_id=stage.id,
                field=f"aggregate.aggregations[{op.output_column}].where", cols=cols,
            ))
    return issues
