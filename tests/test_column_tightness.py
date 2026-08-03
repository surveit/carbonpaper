"""`type` and `nullable` are required on a Column, and the requirement REACHES the
authoring agent.

An agent fills columns in through the `submit_answer` tool, whose input schema is
`target_schema.model_json_schema()` (app/core/agent/agent.py). So a field being
required on the model is only half of it — these pin that it also lands in the
`required` list of the schema the agent is handed, which is the channel that makes
an undecided column unsubmittable rather than silently loosest.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.agent.agent import advertise_more_than_one_argument
from app.models.named_schemas import NamedColumn, SchemaLibrary
from app.models.schema import Column, TableSchema

_OWED = {"type", "nullable"}


def _defs(model) -> dict:
    """A model's `$defs`; a self-referential model (Column.fields) puts its own
    entry there rather than at the top level."""
    return model.model_json_schema()["$defs"]


# ── required on the model ────────────────────────────────────────────────────
@pytest.mark.parametrize("omitted", sorted(_OWED))
def test_a_column_omitting_type_or_nullable_is_refused(omitted):
    spec = {"name": "c", "type": "str", "nullable": True}
    del spec[omitted]
    with pytest.raises(ValidationError) as err:
        Column.model_validate(spec)
    assert omitted in str(err.value)


def test_a_column_stating_both_is_accepted():
    c = Column.model_validate({"name": "c", "type": "str", "nullable": True})
    assert c.type == "str" and c.nullable is True


# ── and required in the schema the authoring agent is handed ─────────────────
def test_column_json_schema_requires_both():
    assert _OWED <= set(_defs(Column)["Column"]["required"])


def test_named_column_json_schema_requires_both():
    assert _OWED <= set(NamedColumn.model_json_schema()["required"])


def test_the_requirement_reaches_the_schema_library_the_data_model_agent_submits():
    # SchemaLibrary is the data-model agent's target_schema, so its $defs are what
    # the agent reads when it writes a column.
    defs = _defs(SchemaLibrary)
    assert _OWED <= set(defs["NamedColumn"]["required"])
    assert _OWED <= set(defs["Column"]["required"])  # nested `fields` sub-columns


def test_the_requirement_reaches_the_submit_answer_tool_input_schema():
    """The end of the chain: the expression `Agent.build_engine` hands the tool as
    its `input_schema` — `advertise_more_than_one_argument(target_schema
    .model_json_schema())` — carries the requirement through unflattened."""
    input_schema = advertise_more_than_one_argument(SchemaLibrary.model_json_schema())
    assert _OWED <= set(input_schema["$defs"]["NamedColumn"]["required"])


# ── the payoff: looseness survives, but only when stated ─────────────────────
def test_a_column_may_still_be_declared_loose_it_just_has_to_say_so():
    # The point is not that every column tightens — an as-filed text column stays
    # `str`/nullable. It is that the declaration says which it is.
    loose = TableSchema(columns=[Column(name="income", type="str", nullable=True)])
    assert loose.columns[0].nullable is True
