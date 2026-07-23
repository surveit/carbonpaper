"""fill_output_schema: the generator write-time auto-fill for a schema-less
join/aggregate stage. Wraps derive_aggregate_output_columns /
derive_join_output_columns (the column-level derivations tested in
test_derive_output_columns.py) with dispatch-by-type and a copy-on-write
step: never partial, never overwriting an authored output_schema, and a
no-op for any stage type with no fill derivation."""
from __future__ import annotations

from app.models.stage import Stage
from app.models.stages import fill_output_schema


def _aggregate_stage(*, output_schema=None, edge_schema="default"):
    """One aggregate stage grouping facilities by company, counting rows.
    `edge_schema` 'default' declares company:str, revenue:int on the input
    edge; None omits the edge schema entirely, making the group_by column
    uncarryable."""
    if edge_schema == "default":
        edge_schema = {
            "columns": [
                {"name": "company", "type": "str"},
                {"name": "revenue", "type": "int"},
            ],
        }
    inputs = [{"id": "facilities"}]
    if edge_schema is not None:
        inputs = [{"id": "facilities", "schema": edge_schema}]
    spec = {
        "id": "totals",
        "name": "Company totals",
        "type": "aggregate",
        "inputs": inputs,
        "aggregate": {
            "group_by": ["company"],
            "aggregations": [{"output_column": "n", "formula": "count"}],
        },
    }
    if output_schema is not None:
        spec["output_schema"] = output_schema
    return Stage.model_validate(spec)


def test_fill_adds_schema_when_absent_and_derivable():
    stage = _aggregate_stage()
    assert stage.output_schema is None

    filled = fill_output_schema(stage)

    assert filled is not stage
    assert filled.output_schema is not None
    names = {c.name for c in filled.output_schema.columns}
    assert names == {"company", "n"}
    company = filled.output_schema.column_for_name("company")
    assert company is not None and company.type == "str"
    n = filled.output_schema.column_for_name("n")
    assert n is not None and n.type == "int"
    # the derived fill never guesses a primary_key
    assert filled.output_schema.primary_key is None

    # the original Stage object's own fields are untouched
    assert stage.output_schema is None
    assert stage.id == "totals"
    assert stage.aggregate is not None
    assert stage.aggregate.group_by == ["company"]


def test_fill_never_overwrites_authored_schema():
    authored = {
        "columns": [
            {"name": "company", "type": "str"},
            {"name": "n", "type": "int"},
        ],
    }
    stage = _aggregate_stage(output_schema=authored)

    filled = fill_output_schema(stage)

    assert filled is stage
    assert filled.output_schema is not None
    assert [c.name for c in filled.output_schema.columns] == ["company", "n"]


def test_fill_skips_underivable():
    # No edge schema at all: group_by's "company" has no edge Column to carry,
    # so the aggregate derivation returns None and the fill is a no-op.
    stage = _aggregate_stage(edge_schema=None)

    filled = fill_output_schema(stage)

    assert filled is stage
    assert filled.output_schema is None


def test_fill_ignores_non_fillable_types():
    llm_stage = Stage.model_validate({
        "id": "ask", "type": "llm_transform", "name": "ask",
        "inputs": [{"id": "src", "schema": {
            "columns": [{"name": "a", "type": "str", "nullable": False}],
            "primary_key": ["a"],
        }}],
        "output_schema": {
            "columns": [{"name": "a", "type": "str", "nullable": False},
                        {"name": "verdict", "type": "str", "nullable": False}],
            "primary_key": ["a"],
        },
        "llm": {"prompt_template": "judge {a}"},
    })
    assert fill_output_schema(llm_stage) is llm_stage

    publish_stage = Stage.model_validate({
        "id": "pub", "type": "publish", "name": "pub", "inputs": ["src"],
        "publish": {},
        "function": {"kind": "inline", "code": "def transform(df, output_dir):\n    return df"},
    })
    assert publish_stage.output_schema is None

    assert fill_output_schema(publish_stage) is publish_stage
