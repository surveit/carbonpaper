"""Handler for the join stage type."""

from __future__ import annotations

from typing import Any

import pandas as pd


def handle_join(stage: dict[str, Any], inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    join_cfg = stage.get("join", {})
    inps = stage.get("inputs", [])
    if len(inps) < 2:
        raise ValueError(f"join stage {stage['id']} needs >=2 inputs")
    left = inputs[inps[0]["id"]]
    right = inputs[inps[1]["id"]]
    keys = join_cfg.get("keys") or join_cfg.get("on") or []
    how = join_cfg.get("type", "inner")
    left_keys = [k["left"] for k in keys]
    right_keys = [k["right"] for k in keys]
    if not left_keys:
        raise ValueError(f"join stage {stage['id']} has no keys configured")

    merged = left.merge(right, left_on=left_keys, right_on=right_keys, how=how, suffixes=("", "_r"))

    select = join_cfg.get("select")
    if select:
        existing = [c for c in select if c in merged.columns]
        merged = merged[existing]
    return merged
