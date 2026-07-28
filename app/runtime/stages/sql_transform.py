"""Handler for the sql_transform stage type."""
from __future__ import annotations

import duckdb
import pandas as pd

from app.models import Stage

from ..context import RunContext


def handle_sql_transform(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    sql = stage.sql
    assert sql is not None  # Stage validation: sql_transform carries sql
    con = _read_only_connection()
    for ref in stage.inputs:
        con.register(ref.id, inputs[ref.id])
    try:
        return con.sql(sql.query).df()
    except duckdb.Error as exc:
        raise ValueError(
            f"stage '{stage.id}': query failed against inputs "
            f"{sorted(inputs)}: {exc}\nquery:\n{sql.query}"
        ) from exc


def _read_only_connection() -> duckdb.DuckDBPyConnection:
    """A fresh in-memory DuckDB connection with file-system and network access
    disabled (COPY/ATTACH/read_csv/httpfs all raise PermissionException) and
    that lockdown itself locked so the query text cannot re-enable it. This
    does NOT sandbox arbitrary compute (UDFs, memory, CPU) — only external I/O."""
    con = duckdb.connect(":memory:")
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    return con
