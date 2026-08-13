"""Key coverage on a join: which reference key values reached no output row, and which
subject key values the reference never lists. Every other check the runtime runs asks
whether the values PRESENT are allowed; an absent key leaves no row behind, so type,
enum, nullability and row-count checks all read clean over it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd
import pyarrow as pa

from app.core.frames import convert_cell_to_json_native, table_to_frame
from app.models import WorkflowStage
from app.models.severity import UserFacingErrorSeverity
from app.models.stages.join import JoinStage

from .validation import Issue

# How many absent key values a message names before it counts the rest.
_SAMPLE_N = 10

# A key value, as one cell per key pair, so a composite key is one tuple.
KeyValues = set[tuple[object, ...]]

# Both lead with the consequence and name no count as a grammatical subject, so one
# wording carries any number of absent keys.
REFERENCE_GAP = (
    "The output holds no row for {absent} of the {total} key values in reference input "
    "'{reference}': {sample}. Nothing was dropped — the subject never carried these keys, "
    "so no other check has a row to read."
)

SUBJECT_GAP = (
    "Reference input '{reference}' lists no match for {absent} of the {total} key values "
    "in subject input '{subject}': {sample}. Those rows hold nulls in the columns this "
    "join lands."
)

UNCOMPARABLE_KEY = (
    "key coverage was not checked: a join key column holds values that cannot be "
    "compared as a set (a list or dict cell)."
)


def find_key_coverage_issues(
    workflow_stage: WorkflowStage, inputs: Mapping[str, pa.Table]
) -> list[Issue]:
    stage = workflow_stage.stage
    if not isinstance(stage, JoinStage):
        return []
    subject_id, reference_id = workflow_stage.inputs[0].id, workflow_stage.inputs[1].id
    if subject_id not in inputs or reference_id not in inputs:
        return []

    left = [key.left for key in stage.join.keys]
    right = [key.right for key in stage.join.keys]
    # Only a join reads keys, so the materialization is paid here rather than by
    # every stage the executor validates.
    subject = _read_key_values(table_to_frame(inputs[subject_id]), left)
    reference = _read_key_values(table_to_frame(inputs[reference_id]), right)
    if subject is None or reference is None:
        return [Issue(UserFacingErrorSeverity.warning, "+".join(left), UNCOMPARABLE_KEY)]

    # `how="left"` means the output's key set IS the subject's, so both gaps are a
    # property of the two inputs and neither needs the frame the merge produced.
    issues = _describe_gap(
        reference - subject, reference, "+".join(right),
        REFERENCE_GAP, reference=reference_id,
    )
    issues += _describe_gap(
        subject - reference, subject, "+".join(left),
        SUBJECT_GAP, subject=subject_id, reference=reference_id,
    )
    return issues


def _describe_gap(
    absent: KeyValues, total: KeyValues, column: str, template: str, **named: str
) -> list[Issue]:
    if not absent:
        return []
    ordered = sorted(absent, key=lambda value: tuple(str(cell) for cell in value))
    return [
        Issue(
            UserFacingErrorSeverity.warning,
            column,
            template.format(
                absent=f"{len(absent):,}",
                total=f"{len(total):,}",
                sample=_describe_sample(ordered),
                **named,
            ),
        )
    ]


def _describe_sample(ordered: Sequence[tuple[object, ...]]) -> str:
    shown = ", ".join(_describe_key(value) for value in ordered[:_SAMPLE_N])
    remaining = len(ordered) - _SAMPLE_N
    return f"{shown}, and {remaining:,} more" if remaining > 0 else shown


def _read_key_values(frame: pd.DataFrame, columns: Sequence[str]) -> KeyValues | None:
    """None where the values cannot form a set; a null key matches nothing, so it is excluded."""
    if any(column not in frame.columns for column in columns):
        return None
    keys = frame[list(columns)]
    present = keys[keys.notna().all(axis=1)]
    try:
        return set(present.itertuples(index=False, name=None))
    except TypeError:
        return None


def _describe_key(value: tuple[object, ...]) -> str:
    cells = [repr(convert_cell_to_json_native(cell)) for cell in value]
    return cells[0] if len(cells) == 1 else "(" + ", ".join(cells) + ")"
