"""derive_aggregate_output_columns / derive_join_output_columns: the
column-level derivations that back both the save-time output_schema check
(derive_*_output_types, reimplemented as thin wrappers over these) and a
later auto-fill of an omitted output_schema. Each returns the handle's fully
specified output `Column` list, or None unless every one is derivable."""
from __future__ import annotations

from app.models import AggregateConfig, AggregationOp, Column, JoinConfig, JoinKey, TableSchema
from app.models.stages.aggregate import derive_aggregate_output_columns, derive_aggregate_output_types
from app.models.stages.join import derive_join_output_columns, derive_join_output_types

_AGG_EDGE = TableSchema(columns=[
    Column(name="company", type="str", nullable=False, enum=["Acme", "Beta"]),
    Column(name="revenue", type="int"),
    Column(name="flag", type="bool"),
])

_LEFT = TableSchema(columns=[
    Column(name="facility_id", type="str", nullable=False),
    Column(name="name", type="str", nullable=False),
])
_RIGHT = TableSchema(columns=[
    Column(name="facility_id", type="str", nullable=False),
    Column(name="name", type="int", nullable=False),
    Column(name="amount", type="int", nullable=False),
])
_KEYS = [JoinKey(left="facility_id", right="facility_id")]


def test_aggregate_columns_carry_edge_spec_for_group_by():
    aggregate = AggregateConfig(
        group_by=["company"],
        aggregations=[AggregationOp(output_column="n", formula="count")],
    )
    columns = derive_aggregate_output_columns(aggregate, _AGG_EDGE)
    assert columns is not None
    company = next(c for c in columns if c.name == "company")
    assert company == _AGG_EDGE.column_for_name("company")
    assert company.type == "str"
    assert company.nullable is False
    assert company.enum == ["Acme", "Beta"]


def test_aggregate_op_columns_are_nullable():
    aggregate = AggregateConfig(
        group_by=["company"],
        aggregations=[
            AggregationOp(output_column="total", formula="sum", value_column="revenue"),
        ],
    )
    columns = derive_aggregate_output_columns(aggregate, _AGG_EDGE)
    assert columns is not None
    total = next(c for c in columns if c.name == "total")
    assert total.type == "int"
    assert total.nullable is True


def test_aggregate_underivable_type_means_no_fill():
    aggregate = AggregateConfig(
        group_by=["company"],
        aggregations=[
            AggregationOp(output_column="total", formula="sum", value_column="flag"),
        ],
    )
    assert derive_aggregate_output_columns(aggregate, _AGG_EDGE) is None


def test_aggregate_no_edge_schema_means_no_fill():
    aggregate = AggregateConfig(
        group_by=["company"],
        aggregations=[AggregationOp(output_column="n", formula="count")],
    )
    assert derive_aggregate_output_columns(aggregate, None) is None


def test_join_right_columns_nullable_under_left_join():
    join = JoinConfig(type="left", keys=_KEYS)
    columns = derive_join_output_columns(join, _LEFT, _RIGHT)
    assert columns is not None
    amount = next(c for c in columns if c.name == "amount")
    assert amount.nullable is True
    name = next(c for c in columns if c.name == "name")
    assert name.nullable is False  # left side keeps its own


def test_join_inner_keeps_source_nullability():
    join = JoinConfig(type="inner", keys=_KEYS)
    columns = derive_join_output_columns(join, _LEFT, _RIGHT)
    assert columns is not None
    amount = next(c for c in columns if c.name == "amount")
    assert amount.nullable is False
    name = next(c for c in columns if c.name == "name")
    assert name.nullable is False


def test_join_collision_renamed_with_source_spec():
    join = JoinConfig(type="inner", keys=_KEYS)
    columns = derive_join_output_columns(join, _LEFT, _RIGHT)
    assert columns is not None
    name_r = next(c for c in columns if c.name == "name_r")
    assert name_r.type == "int"
    assert name_r.nullable is False


def test_join_select_projects_filled_columns():
    join = JoinConfig(type="left", keys=_KEYS, select=["amount", "facility_id"])
    columns = derive_join_output_columns(join, _LEFT, _RIGHT)
    assert columns is not None
    assert [c.name for c in columns] == ["amount", "facility_id"]


def test_join_missing_edge_means_no_fill():
    join = JoinConfig(type="left", keys=_KEYS)
    assert derive_join_output_columns(join, None, _RIGHT) is None
    assert derive_join_output_columns(join, _LEFT, None) is None


def test_types_wrappers_agree_with_columns():
    aggregate = AggregateConfig(
        group_by=["company"],
        aggregations=[
            AggregationOp(output_column="n", formula="count"),
            AggregationOp(output_column="total", formula="sum", value_column="revenue"),
        ],
    )
    agg_columns = derive_aggregate_output_columns(aggregate, _AGG_EDGE)
    assert agg_columns is not None
    assert derive_aggregate_output_types(aggregate, _AGG_EDGE) == {c.name: c.type for c in agg_columns}

    join = JoinConfig(type="left", keys=_KEYS)
    join_columns = derive_join_output_columns(join, _LEFT, _RIGHT)
    assert join_columns is not None
    assert derive_join_output_types(join, _LEFT, _RIGHT) == {c.name: c.type for c in join_columns}
