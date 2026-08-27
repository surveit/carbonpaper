"""every stored workflow output names the kind it always was

Revision ID: 0018
Revises: 0017
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

# `workflow_outputs` became a tagged union of `figure` and `table`, and
# WorkflowFigureRule.kind is required with no default, so every rule written before
# that parses nowhere — and a project's whole shell fails with it, because
# project_state lists its versions. Every stored rule names a `column`, which was
# the only shape there was: they are all figures.
_COLLECTIONS = ("workflow_version", "working_copy", "draft")
_SCHEMA_VERSION = 8
FIGURE = "figure"


def upgrade() -> None:
    connection = op.get_bind()
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            if not tag_stored_outputs(document):
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=?, schema_version=? "
                "WHERE collection=? AND id=?",
                (json.dumps(document), _SCHEMA_VERSION, collection, str(doc_id)),
            )


def downgrade() -> None:
    raise NotImplementedError(
        "0018 is not reversible: a `table` rule has no shape in the model that "
        "predates `kind`, and stripping its tag would leave a figure naming no column"
    )


def tag_stored_outputs(document: Any) -> bool:
    """True if anything changed, so a store already at head is left byte-identical."""
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return False
    tagged = [_tag_one_stage(stage) for stage in stages if isinstance(stage, dict)]
    return any(tagged)


def _tag_one_stage(stage: dict[str, Any]) -> bool:
    rules = stage.get("workflow_outputs")
    if not isinstance(rules, list):
        return False
    tagged = [_tag_one_rule(rule, stage.get("id")) for rule in rules
              if isinstance(rule, dict)]
    return any(tagged)


def _tag_one_rule(rule: dict[str, Any], stage_id: Any) -> bool:
    if "kind" in rule:
        return False
    if "column" not in rule:
        raise ValueError(
            f"workflow output {rule.get('slug')!r} on stage {stage_id!r} names no "
            f"column, so it is not the one-cell output every untagged rule was"
        )
    rule["kind"] = FIGURE
    return True
