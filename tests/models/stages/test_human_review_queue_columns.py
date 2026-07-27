from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import InputRef, QueueConfig, Stage
from app.models.stages import find_config_column_issues


def _queue_stage(*, filter_expr, edge_columns):
    return {
        "id": "wc", "type": "human_review_queue", "name": "wc",
        "inputs": [{"id": "src", "schema": {
            "columns": [{"name": c, "type": "str", "nullable": False} for c in edge_columns],
        }}],
        "output_schema": {"columns": [{"name": c, "type": "str", "nullable": False} for c in edge_columns]},
        "queue": {"filter": filter_expr},
    }


def test_filter_missing_column_rejected():  # argcritic bug 4
    with pytest.raises(ValidationError):
        Stage.model_validate(_queue_stage(
            filter_expr="writer_confirmed == True", edge_columns=["claim_id", "assertion_text"],
        ))


def test_filter_valid_column_clean():
    Stage.model_validate(_queue_stage(
        filter_expr="assertion_text IS NOT NULL", edge_columns=["claim_id", "assertion_text"],
    ))


def test_no_filter_is_clean():
    stage = {
        "id": "wc", "type": "human_review_queue", "name": "wc",
        "inputs": [{"id": "src", "schema": {"columns": [{"name": "claim_id", "type": "str", "nullable": False}]}}],
        "output_schema": {"columns": [{"name": "claim_id", "type": "str", "nullable": False}]},
        "queue": {},
    }
    Stage.model_validate(stage)


def test_no_edge_schema_declared_is_skipped():
    """Without the upstream edge declaring any schema at all, the filter check
    is unresolvable, and skipped rather than flagged. `Stage._schemas_declared`
    rejects an input with no schema, so the edge is stripped with model_copy
    after construction: this pins find_queue_column_issues' own guard, which is
    reached from paths that do not go through a validated Stage."""
    stage = Stage.model_validate(_queue_stage(
        filter_expr="assertion_text IS NOT NULL", edge_columns=["claim_id", "assertion_text"],
    ))
    unresolvable = stage.model_copy(update={
        "inputs": [InputRef(id="src")], "queue": QueueConfig(filter="ghost == 1"),
    })
    assert find_config_column_issues(unresolvable) == []
