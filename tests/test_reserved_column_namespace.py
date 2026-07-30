"""The `_`-prefixed column namespace (`app.models.schema.INTERNAL_COLUMN_PREFIX`)
is the runtime's, not a schema's: the row driver's internal columns, row lineage
and stored-document bookkeeping all spend it, so a stage may never DECLARE a
column there. Enforced on the Stage model, which is what stops the compiler from
ever authoring one."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m
from app.models.schema import INTERNAL_COLUMN_PREFIX
from app.runtime.lineage import TRACE_SOURCE_ROW_KEY, TRACE_SOURCE_STAGE_KEY
from app.runtime.stages.execution import (
    ROW_DEFERRED_KEY,
    ROW_ERROR_KEY,
    ROW_USAGE_KEY,
)
from app.services.node_review import CANONICAL_IGNORE_KEYS


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


# ── every key the machinery spends lives in the reserved namespace ───────────
@pytest.mark.parametrize(
    "key",
    [
        ROW_ERROR_KEY,
        ROW_USAGE_KEY,
        ROW_DEFERRED_KEY,
        TRACE_SOURCE_STAGE_KEY,
        TRACE_SOURCE_ROW_KEY,
        *sorted(CANONICAL_IGNORE_KEYS),
    ],
)
def test_internal_keys_sit_under_the_reserved_prefix(key):
    """If a new internal key were added outside the prefix, reserving the prefix
    would no longer protect it — so each one is asserted to be inside."""
    assert key.startswith(INTERNAL_COLUMN_PREFIX)


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


def test_a_schema_reports_its_own_offending_columns():
    schema = m.TableSchema.model_validate(
        {"columns": [{"name": "a"}, {"name": "_b"}, {"name": "_c"}]}
    )
    assert schema.find_internal_namespace_columns() == ["_b", "_c"]
