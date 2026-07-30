"""A 1:1 stage's inner transform: which input columns it READS, as against the
outer `output_schema` — the whole row on the way out, passthrough columns and all.
Only `reads` is declared; the columns a stage ADDS are derivable (compute_inner_adds),
so asking for them too would just relocate the redundant bookkeeping this exists to
remove. Concept and rationale: docs/architecture.md.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from pydantic import Field

from app.models.schema import TableSchema, _Base

if TYPE_CHECKING:
    from app.models.stage import Stage


class InnerTransform(_Base):
    """What a 1:1 stage does to a row, stated without the passthrough columns."""

    reads: list[str] = Field(
        description=(
            "The input columns this stage's code or prompt actually looks at. Every "
            "other input column is a passthrough the stage never sees. Name only what "
            "is genuinely read — this is the reviewer's answer to \"what can this step "
            "possibly depend on?\", and the runtime holds the code to it by showing it "
            "nothing else."
        ),
    )


def compute_inner_adds(stage: "Stage") -> Optional[TableSchema]:
    """The columns `stage` APPENDS to its input — the inner half of a
    grain-and-order-preserving stage's schema contract, every passthrough column
    excluded. Recovered from the outer schemas by subtraction, which is the single
    definition of that spec for every caller (the LLM runtime's reply model, the
    stage panel, a row function's projected input), so none can drift from another.

    None when `stage` is not grain-and-order-preserving, takes no input, or
    declares no `output_schema` to subtract from."""
    if not stage.is_grain_and_order_preserving or not stage.inputs:
        return None
    input_schema = stage.inputs[0].table_schema
    if stage.output_schema is None or input_schema is None:
        return None
    return stage.output_schema.subtract(input_schema)


def scoped_row_schemas(stage: "Stage") -> Optional[tuple[TableSchema, TableSchema]]:
    """`(input row, expected row)` schemas narrowed to `stage`'s declared read-set:
    what one test case must supply and what it must expect back. None when the
    stage declares no `inner.reads`, in which case a case instances the whole
    schema, passthrough columns and all.

    This is what keeps a test case readable. `parse_reported_money` declares 16
    input columns and reads 2; without narrowing, every case is a 16-column table
    in which 14 columns are noise the reviewer has to look past — and the test
    tables ARE the review surface.

    Both results carry no primary_key: they describe one row's shape, not a table,
    and the read-set need not include the key."""
    reads = stage.inner_reads()
    if reads is None or not stage.inputs:
        return None
    wanted = set(reads)
    input_row = TableSchema(
        columns=[c for c in stage.inputs[0].table_schema.columns if c.name in wanted],
        primary_key=None,
    )
    adds = compute_inner_adds(stage)
    return input_row, TableSchema(
        columns=[*input_row.columns, *(adds.columns if adds else [])],
        primary_key=None,
    )


def find_inner_transform_issues(stage: "Stage") -> list[str]:
    """Every way `stage`'s declared `inner` fails to describe a transform of its
    single input: [] when it declares no `inner`, or its read-set resolves.

    `reads` must name columns the input declares, and must not name one the stage
    ADDS — a stage cannot read its own output, and a read-set naming one is a sign
    the inner and outer halves were confused."""
    inner = stage.inner
    if inner is None:
        return []
    if not stage.is_grain_and_order_preserving:
        return [
            f"stage '{stage.id}': `inner` describes a 1:1 row transform, which type "
            f"`{stage.type}` is not — it may reshape its input, so its output_schema "
            f"is authoritative and there is no inner half to declare"
        ]
    if not stage.inputs:
        return [f"stage '{stage.id}': declares `inner` but takes no input to transform"]

    input_schema = stage.inputs[0].table_schema
    declared = {c.name for c in input_schema.columns}
    # Taken off output_schema by name rather than through compute_inner_adds, which
    # subtracts strictly and would raise on the very schema disagreement that
    # _llm_transform_one_to_one exists to report with a better message.
    outer = stage.output_schema
    added = {c.name for c in outer.columns} - declared if outer else set()
    return [
        f"stage '{stage.id}': inner.reads names {name!r}, which "
        + (
            "this stage ADDS — a stage cannot read its own output"
            if name in added
            else f"its input '{stage.inputs[0].id}' does not declare"
        )
        for name in inner.reads
        if name not in declared
    ]
