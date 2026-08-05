"""name a queue's columns and an enrich's brought columns

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# A migration is frozen in time: it must keep reading v1 documents forever, so
# every name below is a literal copied from the pre-migration code rather than an
# import that would move underneath it.
_QUEUE = "human_review_queue"
_JOINS = ("enrich", "expand")

# What the pre-migration runtime hardcoded when it wrote a review record. Recovered
# from app/runtime/stages/human_review_queue.py at a1aa921e, not inferred from names.
_VERDICT, _REVIEWER, _REVIEWED_AT, _NOTES = (
    "decision", "reviewer_id", "reviewed_at", "review_notes")

# WHICH COLUMN THE REVIEWER REVISES — a human decision, not a fact in the data.
# A v1 queue reviewed no column (its score path keyed off a `score` column no
# project ever had), so nothing on disk answers this. Fill in one entry per
# project, {project: {source column: added column}}, and re-run. Leaving a project
# out fails the migration rather than inventing a reviewed column for it.
_REVIEWED_COLUMNS_BY_PROJECT: dict[str, dict[str, str]] = {
    "anti_activist_hate_on_x": {"severity_tier": "severity_tier_reviewed"},
    "dsa_evidence_capture": {"severity_tier": "severity_tier_reviewed"},
    "hate_on_activist_pages": {
        "final_flag": "final_flag_reviewed",
        "severity_tier": "severity_tier_reviewed",
    },
    "palm_oil_facility_asset": {"review_status": "review_status_reviewed"},
}


class UnmigratableRecord(Exception):
    """A record whose new shape the stored data does not determine."""


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection='workflow_version'"
    ).fetchall()
    undecided: set[str] = set()
    for doc_id, data in rows:
        document = json.loads(data)
        project = str(doc_id).split("/")[0]
        if project not in _REVIEWED_COLUMNS_BY_PROJECT and _has_queue(document):
            undecided.add(project)
            continue
        _upgrade_document(document, project)
        connection.exec_driver_sql(
            "UPDATE documents SET data=?, schema_version=2 "
            "WHERE collection='workflow_version' AND id=?",
            (json.dumps(document), str(doc_id)),
        )
    if undecided:
        raise UnmigratableRecord(
            "queue.reviewed_columns is a human decision and no project below has one: "
            f"{sorted(undecided)}. Add an entry per project to "
            "_REVIEWED_COLUMNS_BY_PROJECT in this revision and re-run."
        )


def downgrade() -> None:
    # v1 named none of these columns, so dropping them back out would be lossless
    # only for records this migration wrote. It is not worth guessing which.
    raise NotImplementedError("0002 is not reversible: v1 carried no column names")


def _has_queue(document: dict[str, Any]) -> bool:
    return any(stage.get("type") == _QUEUE for stage in document.get("stages", []))


def _upgrade_document(document: dict[str, Any], project: str) -> None:
    for stage in document.get("stages", []):
        if stage.get("type") == _QUEUE:
            _name_queue_columns(stage, project)
        elif stage.get("type") in _JOINS:
            _name_brought_columns(stage)


def _name_queue_columns(stage: dict[str, Any], project: str) -> None:
    """Declare the columns the v1 runtime wrote, repairing an output schema that
    omitted one or typed it as something the runtime never wrote."""
    queue = stage.setdefault("queue", {})
    queue["verdict_column"] = _VERDICT
    queue["reviewer_column"] = _REVIEWER
    queue["reviewed_at_column"] = _REVIEWED_AT
    if _NOTES in _column_names(stage.get("output_schema")):
        queue["review_notes_column"] = _NOTES
    queue["reviewed_columns"] = dict(_REVIEWED_COLUMNS_BY_PROJECT[project])
    # The runtime writes every review-record column as text, whatever a v1 schema declared.
    for name in (_VERDICT, _REVIEWER, _REVIEWED_AT, queue.get("review_notes_column")):
        if name:
            _declare_column(stage, name, "str")
    source_columns = _columns_by_name(stage["inputs"][0].get("schema"))
    for source, target in queue["reviewed_columns"].items():
        if source not in source_columns:
            raise UnmigratableRecord(
                f"stage '{stage.get('id')}': reviewed column '{source}' is not on the input"
            )
        _declare_reviewed_column(stage, source_columns[source], target)


def _name_brought_columns(stage: dict[str, Any]) -> None:
    """`enrich_with` is every output column the reference supplied, under its own name."""
    inputs = stage.get("inputs") or []
    if len(inputs) < 2:
        raise UnmigratableRecord(f"stage '{stage.get('id')}': a join needs two inputs")
    subject = _column_names(inputs[0].get("schema"))
    reference = _column_names(inputs[1].get("schema"))
    brought = [name for name in _column_names(stage.get("output_schema"))
               if name not in subject]
    unexplained = [name for name in brought if name not in reference]
    if unexplained:
        raise UnmigratableRecord(
            f"stage '{stage.get('id')}': output column(s) {unexplained} come from "
            "neither input, so which reference column lands as them is not recorded"
        )
    join = stage.setdefault("join", {})
    join["enrich_with"] = {name: name for name in brought}
    # v1 carried the output projection on the join block; v2 reads it off
    # output_schema and forbids the extra key.
    join.pop("select", None)


def _declare_reviewed_column(
    stage: dict[str, Any], source_column: dict[str, Any], target: str
) -> None:
    """Carries the source's WHOLE spec — enum and nullability too, not just its type."""
    columns = stage.setdefault("output_schema", {}).setdefault("columns", [])
    declared = {**source_column, "name": target}
    for index, column in enumerate(columns):
        if column["name"] == target:
            columns[index] = declared
            return
    columns.append(declared)


def _declare_column(stage: dict[str, Any], name: str, type_name: str) -> None:
    """Add the column to the output schema, or correct the type a v1 schema gave it."""
    columns = stage.setdefault("output_schema", {}).setdefault("columns", [])
    for column in columns:
        if column["name"] == name:
            column["type"] = type_name
            return
    columns.append({"name": name, "type": type_name, "nullable": True})


def _column_names(schema: dict[str, Any] | None) -> list[str]:
    return [column["name"] for column in (schema or {}).get("columns", [])]


def _columns_by_name(schema: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {column["name"]: column for column in (schema or {}).get("columns", [])}
