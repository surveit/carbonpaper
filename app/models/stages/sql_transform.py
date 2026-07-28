"""Table-name validation for a sql_transform stage: every declared input id
must be usable as an unquoted DuckDB table name, and every table the query
references must be one of those declared inputs. Cheap because DuckDB can
report a query's referenced tables (`get_table_names`) without any table
actually being registered — no input data needed at validation time."""
from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from app.models.stage import Stage


def find_sql_table_issues(stage: "Stage") -> list[str]:
    """Every table-naming issue in a sql_transform's `sql.query`: a declared
    input id DuckDB cannot address unquoted, a query that fails to parse, or a
    referenced table absent from the stage's declared inputs."""
    sql = stage.sql
    assert sql is not None  # Stage._handle_for_type guarantees this for type="sql_transform"
    input_ids = {ref.id for ref in stage.inputs}
    con = duckdb.connect(":memory:")
    issues = [
        issue for table_id in sorted(input_ids)
        for issue in _find_identifier_issues(con, stage.id, table_id)
    ]
    try:
        referenced = con.get_table_names(sql.query)
    except duckdb.Error as exc:
        issues.append(f"stage '{stage.id}': sql.query does not parse: {exc}")
        return issues
    issues.extend(
        f"stage '{stage.id}': sql.query references table '{name}', which is "
        f"not one of this stage's declared inputs ({sorted(input_ids)})"
        for name in sorted(referenced - input_ids)
    )
    return issues


def _find_identifier_issues(
    con: duckdb.DuckDBPyConnection, stage_id: str, table_id: str
) -> list[str]:
    try:
        names = con.get_table_names(f"SELECT * FROM {table_id}")
    except duckdb.Error:
        return [
            f"stage '{stage_id}': input id '{table_id}' cannot be used as an "
            "unquoted DuckDB table name (likely a reserved keyword)"
        ]
    return [] if names == {table_id} else [
        f"stage '{stage_id}': input id '{table_id}' does not resolve to "
        "itself as a DuckDB table reference"
    ]
