"""a stored noun splits into the row type its rows ARE and the table that HOLDS them

Revision ID: 0021
Revises: 0020
"""
from __future__ import annotations

import json
from typing import Any, Callable

from alembic import op

from app.services.terms import split_pre_row_type_nouns

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_COLLECTION = "terms"
# StoredTerms.SCHEMA_VERSION, which a live save stamps on the same document.
_SPLIT = 2
_FUSED = 1


def upgrade() -> None:
    _rewrite(split_stored_nouns, _SPLIT)


def downgrade() -> None:
    _rewrite(fuse_row_types_back_into_nouns, _FUSED)


def split_stored_nouns(document: dict[str, Any]) -> bool:
    nouns = document.get("nouns")
    if nouns is None:
        return False
    words = split_pre_row_type_nouns(nouns["schemas"])
    document["row_types"] = words.row_types
    document["schemas"] = {"schemas": [t for t in words.schemas if _holds_rows(t)]}
    document["verbs"] = [_drop_spellings(verb) for verb in document.get("verbs", [])]
    del document["nouns"]
    return True


# A v1 word carried its other spellings; one word is now written one way.
def _drop_spellings(verb: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in verb.items() if key != "also_written"}


_WHAT_A_WORD_ALONE_SAID = ("name", "title", "description")


def _holds_rows(table: dict[str, Any]) -> bool:
    return any(value for key, value in table.items() if key not in _WHAT_A_WORD_ALONE_SAID)


def fuse_row_types_back_into_nouns(document: dict[str, Any]) -> bool:
    row_types = document.get("row_types")
    if row_types is None:
        return False
    tables = document["schemas"]["schemas"]
    # The split named the row type after the noun and left the table under that same name.
    table_by_name = {table["name"]: table for table in tables}
    words = {row_type["id"] for row_type in row_types}
    nouns = [_fuse_one(row_type, table_by_name.get(row_type["id"])) for row_type in row_types]
    nouns += [table for table in tables if table["name"] not in words]
    document["nouns"] = {"schemas": nouns}
    document["verbs"] = [{**verb, "also_written": []} for verb in document.get("verbs", [])]
    del document["row_types"], document["schemas"]
    return True


def _fuse_one(row_type: dict[str, Any], table: dict[str, Any] | None) -> dict[str, Any]:
    if table is not None:
        return table
    return {
        "name": row_type["id"],
        "title": row_type["title"],
        # The split glossed a description-less noun with its title; that is no description.
        "description": _description_behind(row_type),
    }


def _description_behind(row_type: dict[str, Any]) -> str | None:
    definition = row_type["definition"]
    return None if definition == row_type["title"] else definition


def _rewrite(rewrite: Callable[[dict[str, Any]], bool], schema_version: int) -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection=?", (_COLLECTION,)
    ).fetchall()
    for doc_id, data in rows:
        document = json.loads(data)
        if not rewrite(document):
            continue
        connection.exec_driver_sql(
            "UPDATE documents SET data=?, schema_version=? WHERE collection=? AND id=?",
            (json.dumps(document), schema_version, _COLLECTION, str(doc_id)),
        )
