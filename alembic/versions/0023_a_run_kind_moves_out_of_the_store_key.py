"""a run's kind moves out of the store key and becomes a field

Revision ID: 0023
Revises: 0022
"""
from __future__ import annotations

import json

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_COLLECTION = "run"
_KINDS = ("runs", "eval_run")


def upgrade() -> None:
    for old_id, document in _read_runs():
        project, kind, run_id = _split_keyed_kind(old_id)
        if kind is None:
            continue
        document["kind"] = kind
        _rewrite(old_id, f"{project}/{run_id}", document)


def downgrade() -> None:
    for old_id, document in _read_runs():
        kind = document.pop("kind", None)
        if kind is None or "/" not in old_id:
            continue
        project, run_id = old_id.split("/", 1)
        _rewrite(old_id, f"{project}/{kind}/{run_id}", document)


def _split_keyed_kind(doc_id: str) -> tuple[str, str | None, str]:
    """None where no segment names a kind: a row written before them, or a torn key."""
    project, _, rest = doc_id.partition("/")
    kind, _, run_id = rest.partition("/")
    if kind not in _KINDS or not run_id:
        return project, None, rest
    return project, kind, run_id


def _read_runs() -> list[tuple[str, dict]]:
    rows = op.get_bind().exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection = ?", (_COLLECTION,)
    ).fetchall()
    return [(row[0], json.loads(row[1])) for row in rows if _is_json(row[1])]


def _is_json(data: str) -> bool:
    """A torn payload keeps its key: nothing here can tell which kind it was."""
    try:
        json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return False
    return True


def _rewrite(old_id: str, new_id: str, document: dict) -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "UPDATE documents SET id = ?, data = ? WHERE collection = ? AND id = ?",
        (new_id, json.dumps(document), _COLLECTION, old_id),
    )
