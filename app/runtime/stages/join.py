"""Handlers for the two join stage types, enrich (m:1) and expand (m:n). Row
provenance is recorded here and is still the RUNTIME working it out rather than
the stage reporting itself: the ordinal columns below ride the frames handed to
the merge and are read back off its result."""

from __future__ import annotations

from typing import Literal, Optional

import numpy as np
import pandas as pd

from app.models import Stage
from app.models.stages.join import JoinStage

from ..context import RunContext
from ..lineage import attach_row_lineage, merged_inputs_lineage
from .execution import narrow_stage

# Ordinal carriers for the merge, dropped before the frame is returned. They sit
# in the reserved `_`-prefixed namespace (app.models.schema.INTERNAL_COLUMN_PREFIX),
# which a stage may never DECLARE a column in — so collision with real input
# data is structurally impossible, not merely unlikely.
JOIN_SUBJECT_ORD_KEY = "_trace_join_subject_ord"
JOIN_REFERENCE_ORD_KEY = "_trace_join_reference_ord"


def handle_enrich(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    # validate="m:1" makes pandas VERIFY the reference is unique on the key
    # rather than trusting the author: a duplicate would otherwise multiply
    # subject rows silently, which is expand's job, not this one's.
    return _join_reference_into_subject(stage, inputs, validate="m:1")


def handle_expand(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    return _join_reference_into_subject(stage, inputs, validate=None)


def _join_reference_into_subject(
    stage: Stage,
    inputs: dict[str, pd.DataFrame],
    validate: Optional[Literal["m:1"]],
) -> pd.DataFrame:
    join_stage = narrow_stage(stage, JoinStage)
    join_cfg = join_stage.join
    subject_id = join_stage.inputs[0].id
    reference_id = join_stage.inputs[1].id
    keys = join_cfg.keys

    # Carry each side's row ordinal through the merge so the pairing can be read
    # back off the result. `.assign` copies, so the caller's frames are untouched.
    subject = inputs[subject_id].assign(
        **{JOIN_SUBJECT_ORD_KEY: np.arange(len(inputs[subject_id]))})
    reference = inputs[reference_id].assign(
        **{JOIN_REFERENCE_ORD_KEY: np.arange(len(inputs[reference_id]))})

    # how="left": every subject row survives, an unmatched one carrying nulls
    # for the reference columns. Dropping rows is filter_rows' job — it records
    # per-row provenance, so the loss stays visible downstream.
    try:
        joined = subject.merge(
            reference,
            left_on=[k.left for k in keys],
            right_on=[k.right for k in keys],
            how="left",
            suffixes=("", "_r"),
            validate=validate,
        )
    except pd.errors.MergeError as exc:
        raise ValueError(_describe_cardinality_failure(join_stage, reference_id, exc)) from exc

    # An unmatched reference ordinal comes back NaN and is recorded as an ABSENT
    # parent — which is what lets a reader tell "no matching row existed" from
    # "matched a row whose columns are null".
    lineage = merged_inputs_lineage([
        (subject_id, joined[JOIN_SUBJECT_ORD_KEY].tolist()),
        (reference_id, joined[JOIN_REFERENCE_ORD_KEY].tolist()),
    ])
    joined = joined.drop(columns=[JOIN_SUBJECT_ORD_KEY, JOIN_REFERENCE_ORD_KEY])

    select = join_cfg.select
    if select:
        existing = [c for c in select if c in joined.columns]
        joined = joined[existing]
    # Attach LAST: `.attrs` does not survive a frame being rebuilt, and both the
    # drop and the projection above rebuild it.
    return attach_row_lineage(joined, lineage)


def _describe_cardinality_failure(
    stage: "JoinStage", reference_id: str, exc: pd.errors.MergeError
) -> str:
    """Why an enrich refused to run, with the three real fixes."""
    # pandas' own message is appended because it names the duplicated key values.
    pairs = ", ".join(f"{k.left}={k.right}" for k in stage.join.keys)
    return (
        f"stage '{stage.id}': enrich requires at most one row of reference input "
        f"'{reference_id}' per key, but the reference repeats a key. Key pairs: {pairs}. "
        f"Fix it by narrowing the key so the reference is unique on it, by aggregating "
        f"the reference to one row per key first, or by using `expand` if the fan-out "
        f"is intended. pandas reported: {exc}"
    )
