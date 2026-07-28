"""sql_transform's table-name validation: find_sql_table_issues, exercised
both directly and through Stage.model_validate (which raises on any issue)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Stage
from app.models.stages.sql_transform import find_sql_table_issues

_XY = [{"name": "x", "type": "int", "nullable": False},
       {"name": "y", "type": "int", "nullable": False}]


def _sql_stage(*, query: str, input_ids: tuple[str, ...] = ("upstream",)):
    return {
        "id": "s", "type": "sql_transform", "name": "s",
        "inputs": [{"id": sid, "schema": {"columns": _XY}} for sid in input_ids],
        "output_schema": {"columns": _XY},
        "sql": {"query": query},
    }


def test_query_over_its_own_declared_input_is_valid():
    Stage.model_validate(_sql_stage(query="SELECT * FROM upstream"))


def test_query_referencing_an_undeclared_table_is_rejected():
    with pytest.raises(ValidationError, match="not one of this stage's declared inputs"):
        Stage.model_validate(_sql_stage(query="SELECT * FROM ghost"))


def test_malformed_query_is_rejected_naming_the_stage():
    with pytest.raises(ValidationError, match="stage 's': sql.query does not parse"):
        Stage.model_validate(_sql_stage(query="SELEC * FROM upstream"))


def test_reserved_word_input_id_is_rejected_at_validation_time():
    stage = _sql_stage(query="SELECT * FROM upstream", input_ids=["upstream", "select"])
    # "select" is snake_case (passes Stage._snake_case) but is a DuckDB
    # reserved keyword, so it can never be read unquoted in a query.
    with pytest.raises(ValidationError, match="cannot be used as an unquoted DuckDB table name"):
        Stage.model_validate(stage)


def test_find_sql_table_issues_empty_for_a_clean_multi_input_query():
    stage = Stage.model_validate(
        _sql_stage(query="SELECT a.x FROM a JOIN b ON a.x = b.x", input_ids=["a", "b"])
    )
    assert find_sql_table_issues(stage) == []
