"""Handlers for the two join stage types, enrich (m:1) and expand (m:n)."""

from __future__ import annotations

from typing import Literal, Optional

import pandas as pd

from app.models import Stage
from app.models.stages.join import JoinStage

from ..context import RunContext
from .execution import narrow_stage


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
    subject = inputs[join_stage.inputs[0].id]
    reference_id = join_stage.inputs[1].id
    reference = inputs[reference_id]
    keys = join_cfg.keys
    # The reference is narrowed to its key columns plus `bring` BEFORE the
    # merge, so no un-brought reference column can reach the output. A brought
    # column never collides with a subject column (validation refuses that at
    # save); a right KEY sharing a subject column's name still can, so pandas
    # suffixes that one copy and the projection below drops it.
    right_keys = [k.right for k in keys]
    narrowed = reference[list(dict.fromkeys([*right_keys, *join_cfg.bring]))]
    # how="left": every subject row survives, an unmatched one carrying nulls
    # for the brought columns. Dropping rows is filter_rows' job — it records
    # per-row provenance, so the loss stays visible downstream.
    try:
        joined = subject.merge(
            narrowed,
            left_on=[k.left for k in keys],
            right_on=right_keys,
            how="left",
            suffixes=("", "_r"),
            validate=validate,
        )
    except pd.errors.MergeError as exc:
        raise ValueError(_describe_cardinality_failure(join_stage, reference_id, exc)) from exc
    return joined[[*subject.columns, *join_cfg.bring]]


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
