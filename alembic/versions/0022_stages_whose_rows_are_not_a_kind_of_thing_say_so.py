"""a report's output rows and an ungrouped aggregate's are not a kind of thing, and say so

Revision ID: 0022
Revises: 0021
"""
from __future__ import annotations

import json
from typing import Any, Callable

from alembic import op

from app.models.row_types import NO_KIND_ROW_TYPE_ID

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

# Every collection that embeds stage specs; a draft becomes a working copy becomes a version.
_COLLECTIONS = ("workflow_version", "working_copy", "draft")
_ROW_TYPE_ID = "row_type_id"


def upgrade() -> None:
    _rewrite(answer_no_kind_where_the_type_settles_it)


def downgrade() -> None:
    _rewrite(strip_the_backfilled_answer)


def answer_no_kind_where_the_type_settles_it(document: dict[str, Any]) -> bool:
    """True if anything changed, so a store already at head is left byte-identical."""
    # A generator would stop at the first stage it changed.
    answered = [_answer_one_stage(stage) for stage in _list_stage_dicts(document)]
    return any(answered)


def _answer_one_stage(stage: dict[str, Any]) -> bool:
    if _ROW_TYPE_ID in stage or not _is_of_no_kind(stage):
        return False
    stage[_ROW_TYPE_ID] = NO_KIND_ROW_TYPE_ID
    return True


def _is_of_no_kind(stage: dict[str, Any]) -> bool:
    if stage.get("type") == "report":
        return True
    return stage.get("type") == "aggregate" and not _read_group_by(stage)


def _read_group_by(stage: dict[str, Any]) -> list[Any]:
    block = stage.get("aggregate")
    if not isinstance(block, dict) or not isinstance(block.get("group_by"), list):
        raise ValueError(
            f"aggregate stage {stage.get('id')!r} stores no `group_by`, so nothing here "
            f"can tell one row per group from one figure about the whole population"
        )
    group_by: list[Any] = block["group_by"]
    return group_by


def strip_the_backfilled_answer(document: dict[str, Any]) -> bool:
    """Only `no_kind`: a word on a stage was authored, never written by this revision."""
    # A generator would stop at the first stage it changed.
    stripped = [_strip_one_stage(stage) for stage in _list_stage_dicts(document)]
    return any(stripped)


def _strip_one_stage(stage: dict[str, Any]) -> bool:
    if stage.get(_ROW_TYPE_ID) != NO_KIND_ROW_TYPE_ID:
        return False
    del stage[_ROW_TYPE_ID]
    return True


def _list_stage_dicts(document: dict[str, Any]) -> list[dict[str, Any]]:
    stages = document.get("stages")
    if not isinstance(stages, list):
        return []
    return [stage for stage in stages if isinstance(stage, dict)]


def _rewrite(rewrite: Callable[[dict[str, Any]], bool]) -> None:
    connection = op.get_bind()
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            if not rewrite(document):
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=? WHERE collection=? AND id=?",
                (json.dumps(document), collection, str(doc_id)),
            )
