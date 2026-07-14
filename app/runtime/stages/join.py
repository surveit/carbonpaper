"""Handler for the join stage type."""

from __future__ import annotations

from typing import Any, Literal

import pandas as pd

from app.core.models import JoinType, Stage


def handle_join(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    join_cfg = stage.join
    assert join_cfg is not None  # Stage validation: join carries join_cfg
    left = inputs[stage.inputs[0].id]
    right = inputs[stage.inputs[1].id]
    keys = join_cfg.keys or join_cfg.on or []
    left_keys = [k.left for k in keys]
    right_keys = [k.right for k in keys]

    # join_cfg.type is JoinType's declared static type, but use_enum_values
    # already makes it a plain str at runtime; the match narrows it to the
    # Literal pandas' merge(how=...) expects, one arm per JoinType member.
    match join_cfg.type:
        case JoinType.inner:
            how: Literal["inner", "left", "right", "outer"] = "inner"
        case JoinType.left:
            how = "left"
        case JoinType.right:
            how = "right"
        case JoinType.outer:
            how = "outer"

    merged = left.merge(right, left_on=left_keys, right_on=right_keys, how=how, suffixes=("", "_r"))

    select = join_cfg.select
    if select:
        existing = [c for c in select if c in merged.columns]
        merged = merged[existing]
    return merged
