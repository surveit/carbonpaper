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
    """A minimal python_row_function stage, so a test varies only its schemas."""
    return {
        "id": "t",
        "name": "t",
        "type": "python_row_function",
        "inputs": [{"id": "up", "schema": {"columns": input_columns or [{"name": "id"}]}}],
        "output_schema": output_schema,
        "function": {
            "kind": "inline",
            "code": "def transform(row: dict) -> dict:\n    return row\n",
        },
    }


# ── a stage may not declare a column there ──────────────────────────────────
def test_output_schema_column_with_a_leading_underscore_is_refused():
    with pytest.raises(ValidationError) as err:
        m.Stage.model_validate(
            _row_function({"columns": [{"name": "id"}, {"name": "_error"}]})
        )
    assert "_error" in str(err.value)
    assert "reserved" in str(err.value)


def test_an_input_edge_column_with_a_leading_underscore_is_refused():
    with pytest.raises(ValidationError) as err:
        m.Stage.model_validate(
            _row_function(
                {"columns": [{"name": "id"}]},
                input_columns=[{"name": "id"}, {"name": "_usage"}],
            )
        )
    assert "input `up`" in str(err.value)
    assert "_usage" in str(err.value)


def test_a_column_named_only_with_an_underscore_is_refused():
    """Not just the keys the runtime happens to use today — the whole namespace."""
    with pytest.raises(ValidationError):
        m.Stage.model_validate(
            _row_function({"columns": [{"name": "id"}, {"name": "_anything"}]})
        )


def test_an_underscore_inside_a_column_name_is_fine():
    stage = m.Stage.model_validate(
        _row_function({"columns": [{"name": "id"}, {"name": "issue_area"}]})
    )
    assert [c.name for c in stage.output_schema.columns] == ["id", "issue_area"]


def test_an_underscore_prefixed_key_nested_in_a_json_column_is_fine():
    """A key inside a json object is a value on the frame's cell, not a column on
    the frame, so it collides with no machinery."""
    stage = m.Stage.model_validate(
        _row_function(
            {
                "columns": [
                    {"name": "id"},
                    {
                        "name": "payload",
                        "type": "json",
                        "fields": [{"name": "_raw", "type": "str"}],
                    },
                ]
            }
        )
    )
    assert stage.output_schema.columns[1].fields[0].name == "_raw"


def test_validate_stage_reports_it_as_a_non_fatal_issue():
    """The compiler's own gate: `validate_stage` surfaces it as an issue string
    rather than raising."""
    issues = m.validate_stage(
        _row_function({"columns": [{"name": "id"}, {"name": "_error"}]})
    )
    assert any("_error" in issue for issue in issues)


def test_a_plain_table_schema_is_indifferent_to_the_prefix():
    """The ban belongs to the STAGE contract, not to schema primitives: a
    TableSchema on its own knows nothing about the runtime and validates fine."""
    schema = m.TableSchema.model_validate({"columns": [{"name": "a"}, {"name": "_b"}]})
    assert [c.name for c in schema.columns] == ["a", "_b"]


def test_every_offending_column_is_reported_not_just_the_first():
    issues = m.validate_stage(
        _row_function({"columns": [{"name": "id"}, {"name": "_b"}, {"name": "_c"}]})
    )
    joined = " ".join(issues)
    assert "'_b'" in joined and "'_c'" in joined
