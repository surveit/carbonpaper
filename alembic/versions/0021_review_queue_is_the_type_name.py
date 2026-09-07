"""the `human_review_queue` stage type becomes `review_queue`

Revision ID: 0021
Revises: 0020
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

from app.models.stage import parse_stage

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

WAS = "human_review_queue"
NOW = "review_queue"

_SPEC_COLLECTIONS = ("workflow_version", "working_copy")
# A run names the type it executed on `stage_records[].type`, and keeps the queue's
# per-stage counts under a key that carried the old name.
_RUN_COLLECTION = "run"
_WAS_STATS_KEY = "human_review_queue_stats"
_NOW_STATS_KEY = "review_queue_stats"
_SCHEMA_VERSION = 9

# The type is hashed into a stage's definition fingerprint, and these look a row
# up by it. Renaming without moving them asks every judged row again.
_FINGERPRINT_COLLECTIONS = ("review_decision", "queue_fingerprints")
# The one of them holding something no rerun can produce again.
_DECISIONS = "review_decision"


def upgrade() -> None:
    _rename(WAS, NOW, _WAS_STATS_KEY, _NOW_STATS_KEY, _SCHEMA_VERSION)


def downgrade() -> None:
    _rename(NOW, WAS, _NOW_STATS_KEY, _WAS_STATS_KEY, 8)


def _rename(was: str, now: str, was_stats: str, now_stats: str, schema_version: int) -> None:
    moves = _find_fingerprint_moves(was, now)
    _rewrite(_SPEC_COLLECTIONS, lambda d: _rename_stage_specs(d, was, now), schema_version)
    _rewrite((_RUN_COLLECTION,), lambda d: _rename_run(d, was, now, was_stats, now_stats), None)
    _move_cache_entries(moves)
    _rewrite(_FINGERPRINT_COLLECTIONS, lambda d: _move_fingerprint(d, moves), None)


# ── the fingerprint move ─────────────────────────────────────────────────────
def _find_fingerprint_moves(was: str, now: str) -> dict[str, str]:
    """Old definition fingerprint -> new one, for every stored queue stage."""
    moves: dict[str, str] = {}
    unreadable: list[str] = []
    for collection in _SPEC_COLLECTIONS:
        for doc_id, document in _read(collection):
            for spec in _stage_specs(document):
                if spec.get("type") not in (was, now):
                    continue
                pair = _fingerprint_pair(spec, was, now)
                if pair is None:
                    unreadable.append(f"{doc_id}:{spec.get('id')}")
                    continue
                moves[pair[0]] = pair[1]
    _refuse_stranded_decisions(unreadable, moves)
    return moves


def _fingerprint_pair(spec: dict[str, Any], was: str, now: str) -> tuple[str, str] | None:
    """`model_copy` swaps the hashed type without revalidating the Literal it pins."""
    try:
        stage = parse_stage({**spec, "type": now})
    except ValueError:
        return None
    return (
        stage.model_copy(update={"type": was}).compute_definition_fingerprint(),
        stage.compute_definition_fingerprint(),
    )


def _refuse_stranded_decisions(unreadable: list[str], moves: dict[str, str]) -> None:
    """Cache and halt bookkeeping for a version that no longer loads is already dead."""
    if not unreadable:
        return
    stranded = [
        row_id for row_id, document in _read(_DECISIONS)
        if document.get("stage_fingerprint") not in moves
    ]
    if stranded:
        raise ValueError(
            f"{len(unreadable)} stored queue stages no longer parse, so their "
            f"fingerprints cannot be moved, and {len(stranded)} recorded decisions "
            f"are keyed by one of them: {sorted(unreadable)[:5]}. A decision is not "
            "recomputable — repair those stage specs before migrating."
        )


def _move_cache_entries(moves: dict[str, str]) -> None:
    """The cache id embeds the fingerprint, so the row is re-keyed, not just edited."""
    connection = op.get_bind()
    for old, new in moves.items():
        if old == new:
            continue
        for row_id, document in _read("stage_cache", like=f"%/{old}/%"):
            document["stage_fingerprint"] = new
            document["id"] = row_id.replace(f"/{old}/", f"/{new}/")
            connection.exec_driver_sql(
                "UPDATE documents SET id=?, data=? WHERE collection='stage_cache' AND id=?",
                (document["id"], json.dumps(document), row_id),
            )


def _move_fingerprint(document: Any, moves: dict[str, str]) -> bool:
    moved = moves.get(document.get("stage_fingerprint")) if isinstance(document, dict) else None
    if moved is None or moved == document["stage_fingerprint"]:
        return False
    document["stage_fingerprint"] = moved
    return True


# ── the type name itself ─────────────────────────────────────────────────────
def _rename_stage_specs(document: Any, was: str, now: str) -> bool:
    return any([_rename_type(spec, was, now) for spec in _stage_specs(document)])


def _rename_run(document: Any, was: str, now: str, was_stats: str, now_stats: str) -> bool:
    if not isinstance(document, dict):
        return False
    records = document.get("stage_records")
    renamed = [
        _rename_type(record, was, now)
        for record in (records if isinstance(records, list) else [])
        if isinstance(record, dict)
    ]
    if was_stats in document:
        document[now_stats] = document.pop(was_stats)
        return True
    return any(renamed)


def _rename_type(spec: Any, was: str, now: str) -> bool:
    if not isinstance(spec, dict) or spec.get("type") != was:
        return False
    spec["type"] = now
    return True


# ── the store ────────────────────────────────────────────────────────────────
def _stage_specs(document: Any) -> list[dict[str, Any]]:
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return []
    return [stage for stage in stages if isinstance(stage, dict)]


def _read(collection: str, like: str | None = None) -> list[tuple[str, Any]]:
    connection = op.get_bind()
    sql = "SELECT id, data FROM documents WHERE collection=?"
    parameters: tuple[Any, ...] = (collection,)
    if like is not None:
        sql += " AND id LIKE ?"
        parameters += (like,)
    return [
        (str(doc_id), json.loads(data))
        for doc_id, data in connection.exec_driver_sql(sql, parameters).fetchall()
    ]


def _rewrite(collections: tuple[str, ...], rewrite: Any, schema_version: int | None) -> None:
    connection = op.get_bind()
    for collection in collections:
        for doc_id, document in _read(collection):
            if not rewrite(document):
                continue
            if schema_version is None:
                connection.exec_driver_sql(
                    "UPDATE documents SET data=? WHERE collection=? AND id=?",
                    (json.dumps(document), collection, doc_id),
                )
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=?, schema_version=? WHERE collection=? AND id=?",
                (json.dumps(document), schema_version, collection, doc_id),
            )
