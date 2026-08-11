"""A stage may never DECLARE a column in the `_`-prefixed namespace the runtime
spends on machinery (`app.models.stages.shared.INTERNAL_COLUMN_PREFIX`) — refused
on the Stage model, which is what stops the compiler from ever authoring one. The
converse half (every internal key the machinery spends is INSIDE that namespace)
is tests/arch/test_internal_columns_are_prefixed.py."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m


def _row_function(output_schema: dict, input_columns: list[dict] | None = None) -> dict:
    edge = input_columns or [{"name": "id", "type": "str", "nullable": True}]
    # A row function only adds, so whatever `output_schema` names beyond the
    # input edge becomes the signature's adds.
    flowing = {c["name"] for c in edge}
    return {
        "id": "t",
        "description": "t",
        "type": "python_row_function",
        "inputs": [{"id": "up", "schema": {"columns": edge}}],
        "signature": {"form": "extends",
                      "adds": [c for c in output_schema["columns"]
                               if c["name"] not in flowing]},
        "function": {
            "kind": "inline",
            "code": "def transform(row: dict) -> dict:\n    return row\n",
        },
    }


# ── a stage may not declare a column there ──────────────────────────────────
def test_output_schema_column_with_a_leading_underscore_is_refused():
    with pytest.raises(ValidationError) as err:
        m.parse_stage(
            _row_function({"columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "_error", "type": "str", "nullable": True}]})
        )
    assert "_error" in str(err.value)
    assert "reserved" in str(err.value)


def test_an_input_edge_column_with_a_leading_underscore_is_refused():
    with pytest.raises(ValidationError) as err:
        m.parse_stage(
            _row_function(
                {"columns": [{"name": "id", "type": "str", "nullable": True}]},
                input_columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "_usage", "type": "str", "nullable": True}],
            )
        )
    assert "input `up`" in str(err.value)
    assert "_usage" in str(err.value)


def test_a_column_named_only_with_an_underscore_is_refused():
    with pytest.raises(ValidationError):
        m.parse_stage(
            _row_function({"columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "_anything", "type": "str", "nullable": True}]})
        )


def test_an_underscore_inside_a_column_name_is_fine():
    stage = m.parse_stage(
        _row_function({"columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "issue_area", "type": "str", "nullable": True}]})
    )
    assert [c.name for c in stage.resolve_output_schema().columns] == ["id", "issue_area"]


def test_an_underscore_prefixed_key_nested_in_a_json_column_is_fine():
    stage = m.parse_stage(
        _row_function(
            {
                "columns": [
                    {"name": "id", "type": "str", "nullable": True},
                    {
                        "name": "payload",
                        "type": "json",
                        "fields": [{"name": "_raw", "type": "str", "nullable": True}],
                    "nullable": True},
                ]
            }
        )
    )
    assert stage.resolve_output_schema().columns[1].fields[0].name == "_raw"


def test_validate_stage_reports_it_as_a_non_fatal_issue():
    issues = m.validate_stage(
        _row_function({"columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "_error", "type": "str", "nullable": True}]})
    )
    assert any("_error" in issue for issue in issues)


def test_a_plain_table_schema_is_indifferent_to_the_prefix():
    schema = m.TableSchema.model_validate({"columns": [{"name": "a", "type": "str", "nullable": True}, {"name": "_b", "type": "str", "nullable": True}]})
    assert [c.name for c in schema.columns] == ["a", "_b"]


def test_every_offending_column_is_reported_not_just_the_first():
    issues = m.validate_stage(
        _row_function({"columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "_b", "type": "str", "nullable": True}, {"name": "_c", "type": "str", "nullable": True}]})
    )
    joined = " ".join(issues)
    assert "'_b'" in joined and "'_c'" in joined
