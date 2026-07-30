"""Handlers for the two merge stage types, `enrich` and `expand`. Both merge
LEFT, so no subject row is ever dropped here; they differ only in the
cardinality permitted, which `enrich` asks pandas to VERIFY rather than trust."""

from __future__ import annotations

from typing import Literal

import pandas as pd

from app.models import Stage

from ..context import RunContext


def handle_enrich(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    """At most one reference row per subject row, so row count and order come out unchanged."""
    # validate="m:1" makes pandas CHECK that rather than letting a duplicated
    # reference key silently multiply rows. The failure names the offending key,
    # which is the actionable form of "your key is under-specified".
    return _merge(stage, inputs, validate="m:1")


def handle_expand(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    """The reference may repeat, so rows may multiply — the fan-out is the declared intent."""
    return _merge(stage, inputs, validate=None)


def _merge(
    stage: Stage, inputs: dict[str, pd.DataFrame],
    validate: Literal["m:1"] | None,
) -> pd.DataFrame:
    cfg = stage.join
    assert cfg is not None  # Stage validation: a merge type carries the join cfg
    subject = inputs[stage.inputs[0].id]
    reference = inputs[stage.inputs[1].id]

    merged = subject.merge(
        reference,
        left_on=[k.subject for k in cfg.keys],
        right_on=[k.reference for k in cfg.keys],
        how="left",
        suffixes=("", "_r"),
        validate=validate,
    )

    if cfg.select:
        merged = merged[[c for c in cfg.select if c in merged.columns]]
    return merged
