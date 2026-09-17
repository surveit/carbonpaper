"""backfill `project_id` onto every stored workflow_output citation

Revision ID: 0021
Revises: 0020
"""
from __future__ import annotations

import json

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    projects_by_run = _read_projects_by_run(connection)
    for row_id, data in _read_rows(connection, "workflow_output"):
        citation = data.get("citation")
        if not isinstance(citation, dict) or "project_id" in citation:
            continue
        run_id = citation.get("run_id")
        if run_id not in projects_by_run:
            raise RuntimeError(
                f"workflow_output {row_id} cites run {run_id!r}, which no stored run names")
        citation["project_id"] = projects_by_run[run_id]
        _write_row(connection, "workflow_output", row_id, data)


def downgrade() -> None:
    connection = op.get_bind()
    for row_id, data in _read_rows(connection, "workflow_output"):
        citation = data.get("citation")
        if isinstance(citation, dict) and citation.pop("project_id", None) is not None:
            _write_row(connection, "workflow_output", row_id, data)


def _read_projects_by_run(connection) -> dict[str, str]:
    return {
        row_id: json.loads(data)["project"]
        for row_id, data in connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection='run'"
        ).fetchall()
    }


def _read_rows(connection, collection: str) -> list[tuple[str, dict]]:
    rows = connection.exec_driver_sql(
        f"SELECT id, data FROM documents WHERE collection='{collection}'"
    ).fetchall()
    return [(row_id, json.loads(data)) for row_id, data in rows]


def _write_row(connection, collection: str, row_id: str, data: dict) -> None:
    connection.exec_driver_sql(
        "UPDATE documents SET data = %s WHERE collection = %s AND id = %s"
        .replace("%s", "?"),
        (json.dumps(data), collection, row_id),
    )
