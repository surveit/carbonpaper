"""Handlers for the three declared reshapes — explode, dedupe, sort_rank.
Each reports the per-row provenance a python_frame_function doing the same work
cannot, so `app.runtime.trace` crosses the stage. Arrow throughout: none of them
hands a frame to authored code, so none needs pandas.
"""
from __future__ import annotations

from typing import Sequence

import pyarrow as pa
import pyarrow.compute as pc

from app.core.frames import convert_cell_to_json_value
from app.models import WorkflowStage
from app.models.errors import StepRefused
from app.models.stages.dedupe import DedupeKeep, DedupeStage
from app.models.stages.explode import ExplodeStage
from app.models.stages.sort_rank import NullPlacement, SortKey, SortRankStage

from ..context import RunContext
from ..lineage import single_parent_lineage
from ..stage_output import StageOutput
from .agreement import refuse_a_disagreeing_group
from .execution import narrow_stage


def handle_explode(
    workflow_stage: WorkflowStage, inputs: dict[str, pa.Table], ctx: RunContext
) -> StageOutput:
    stage = narrow_stage(workflow_stage, ExplodeStage)
    input_id = workflow_stage.inputs[0].id
    table = inputs[input_id]
    column = stage.explode.column

    lists = table.column(column).combine_chunks()
    if stage.explode.keep_empty:
        lists = _one_null_element_where_empty(lists)
    # list_parent_indices IS the lineage: element k of the flattened column came
    # from row parent_indices[k], and an empty list contributes no element.
    parents = pc.list_parent_indices(lists)
    exploded = table.drop_columns([column]).take(parents).append_column(
        table.field(column).with_type(lists.type.value_type), pc.list_flatten(lists)
    )
    return StageOutput(
        _in_declared_column_order(exploded, table.column_names),
        lineage=single_parent_lineage(input_id, parents.to_pylist()),
    )


def handle_dedupe(
    workflow_stage: WorkflowStage, inputs: dict[str, pa.Table], ctx: RunContext
) -> StageOutput:
    stage = narrow_stage(workflow_stage, DedupeStage)
    input_id = workflow_stage.inputs[0].id
    config = stage.dedupe
    table = inputs[input_id]

    ranked = _ranking_for_keep(table, config.keep, config.by)
    groups_as_ordinals = _group_ordinals(table, config.keys, ranked)
    if config.keep == DedupeKeep.agree:
        _refuse_a_disagreeing_group(table, config.keys, groups_as_ordinals, stage.id)
    # Each group is ranked, so its [0] is always the row `keep` chose to survive.
    kept_ordinals = [ordinals[0] for ordinals in groups_as_ordinals]
    # `ranked` left these in keep order, not the input's.
    kept_in_input_order = sorted(kept_ordinals)
    return StageOutput(
        table.take(kept_in_input_order),
        lineage=single_parent_lineage(input_id, kept_in_input_order),
    )


def handle_sort_rank(
    workflow_stage: WorkflowStage, inputs: dict[str, pa.Table], ctx: RunContext
) -> StageOutput:
    stage = narrow_stage(workflow_stage, SortRankStage)
    input_id = workflow_stage.inputs[0].id
    config = stage.sort_rank
    table = inputs[input_id]

    order = _sorted_row_indices(table, config.keys, stage.id)
    ordered = table.take(order)
    if config.rank_column:
        ordered = ordered.append_column(
            config.rank_column, pa.array(range(1, ordered.num_rows + 1), pa.int64())
        )
    return StageOutput(ordered, lineage=single_parent_lineage(input_id, order.to_pylist()))


# ── explode ──────────────────────────────────────────────────────────────────
def _one_null_element_where_empty(lists: pa.Array) -> pa.Array:
    """`keep_empty`: a row that found nothing still reaches the output, carrying null."""
    empty = pc.or_(pc.is_null(lists), pc.equal(pc.list_value_length(lists), 0))
    one_null = pa.array([[None]] * len(lists), type=lists.type)
    return pc.if_else(empty, one_null, lists)


def _in_declared_column_order(table: pa.Table, names: Sequence[str]) -> pa.Table:
    return table.select(list(names))


# ── dedupe ───────────────────────────────────────────────────────────────────
def _ranking_for_keep(
    table: pa.Table, keep: DedupeKeep, by: str | None
) -> pa.Array | None:
    """Each row's position in the keep order, so the survivor is the group's minimum."""
    if keep in (DedupeKeep.first, DedupeKeep.agree):
        return None
    direction = "ascending" if keep == DedupeKeep.lowest else "descending"
    return pc.sort_indices(table, sort_keys=[(by, direction)])


def _group_ordinals(
    table: pa.Table, keys: Sequence[str], ranked: pa.Array | None
) -> list[list[int]]:
    """Input ordinals per key group, winner first — [0] survives, the rest were collapsed."""
    order = range(table.num_rows) if ranked is None else ranked.to_pylist()
    key_columns = [table.column(key).to_pylist() for key in keys]
    groups: dict[tuple[object, ...], list[int]] = {}
    for ordinal in order:
        groups.setdefault(tuple(column[ordinal] for column in key_columns), []).append(int(ordinal))
    return list(groups.values())


def _refuse_a_disagreeing_group(
    table: pa.Table, keys: Sequence[str], groups: list[list[int]], stage_id: str
) -> None:
    """Checked before a survivor is taken: a group that disagrees has no survivor to take."""
    carried = [name for name in table.column_names if name not in set(keys)]
    if not carried:
        return
    columns = [table.column(name).to_pylist() for name in carried]
    for members in groups:
        if len(members) == 1:
            continue
        refuse_a_disagreeing_group(
            [[column[ordinal] for column in columns] for ordinal in members], carried,
            stage_id=stage_id,
            subject=_name_the_duplicates(table, keys, members[0], len(members)))


def _name_the_duplicates(
    table: pa.Table, keys: Sequence[str], ordinal: int, members: int
) -> str:
    named = ", ".join(
        f"{key}={convert_cell_to_json_value(table.column(key)[ordinal].as_py())!r}"
        for key in keys
    )
    return f"the {members} rows with {named}"


# ── sort_rank ────────────────────────────────────────────────────────────────
def _sorted_row_indices(
    table: pa.Table, keys: Sequence[SortKey], stage_id: str
) -> pa.Array:
    ranked = table
    sort_keys: list[tuple[str, str, str]] = []
    for position, key in enumerate(keys):
        placement = "at_start" if key.nulls == NullPlacement.first else "at_end"
        if key.order is None:
            direction = "descending" if key.descending else "ascending"
            sort_keys.append((key.column, direction, placement))
            continue
        # Applied as a position rather than a comparison on the values, so the
        # sort never falls back to ordering the strings against each other.
        name = f"_trace_reshape_key{position}"
        ranked = ranked.append_column(
            name, _positions_in_stated_order(table.column(key.column), key, stage_id))
        sort_keys.append((name, "ascending", placement))
    return pc.sort_indices(ranked, sort_keys=sort_keys)


def _positions_in_stated_order(
    values: pa.ChunkedArray, key: SortKey, stage_id: str
) -> pa.Array:
    stated = pa.array([str(v) for v in key.order or ()], pa.string())
    positions = pc.index_in(pc.cast(values, pa.string()), value_set=stated)
    # A null value carries its own placement, so only a PRESENT value that the
    # stated order does not name is unranked.
    unmatched = pc.and_(pc.is_null(positions), pc.is_valid(values))
    unranked = pc.unique(pc.filter(values, unmatched)).to_pylist()
    if unranked:
        raise StepRefused(
            f"stage {stage_id}: column `{key.column}` holds {sorted(map(str, unranked))}, "
            f"which its stated `order` {list(key.order or ())} does not rank. A value the "
            f"rule never anticipated must not be sorted to an end and numbered as though "
            f"it had been."
        )
    return positions
