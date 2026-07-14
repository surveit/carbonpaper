"""Handler for the join stage type."""

from __future__ import annotations

from typing import Any, Literal

import pandas as pd

from app.models import JoinType, Stage

from ..lineage import Edge, record_edges

# Hidden positional columns injected before the merge so each output row can be
# traced back to the left/right input row(s) it came from. Named to avoid
# colliding with any real column (and with pandas' `_r` suffix).
_LPOS = "__lineage_left_pos__"
_RPOS = "__lineage_right_pos__"


def handle_join(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    join_cfg = stage.join
    assert join_cfg is not None  # Stage validation: join carries join_cfg
    left_id = stage.inputs[0].id
    right_id = stage.inputs[1].id
    left = inputs[left_id].reset_index(drop=True)
    right = inputs[right_id].reset_index(drop=True)
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

    # Carry each side's row position through the merge so we can reconstruct the
    # output→input edges afterwards. A join reshapes rows (fan-out on many-to-many
    # keys, drops on inner/one-sided), so output-row position is not input-row
    # position and lineage must be recorded rather than assumed positional.
    left_pos = left.assign(**{_LPOS: range(len(left))})
    right_pos = right.assign(**{_RPOS: range(len(right))})
    merged = left_pos.merge(
        right_pos, left_on=left_keys, right_on=right_keys, how=how, suffixes=("", "_r")
    )

    edges: list[Edge] = []
    for out_row, (lpos, rpos) in enumerate(zip(merged[_LPOS], merged[_RPOS])):
        # An outer/left/right join leaves the unmatched side NaN — no edge for it.
        if pd.notna(lpos):
            edges.append((out_row, left_id, int(lpos)))
        if pd.notna(rpos):
            edges.append((out_row, right_id, int(rpos)))
    record_edges(ctx, stage.id, edges)

    merged = merged.drop(columns=[_LPOS, _RPOS])

    select = join_cfg.select
    if select:
        existing = [c for c in select if c in merged.columns]
        merged = merged[existing]
    return merged
