"""Tests for the human_review_queue stage's config-column check
(app/models/stages/human_review_queue.py, wired into
Stage._config_columns_resolve): every column `queue.filter` references must
resolve against the stage's own input edge schema — the check that catches a
filter reading a column the review is meant to SET (argcritic bug 4), now
enforced at Stage construction rather than only at workflow load."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Stage


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
    is unresolvable, and skipped rather than flagged."""
    stage = {
        "id": "wc", "type": "human_review_queue", "name": "wc", "inputs": ["src"],
        "output_schema": {"columns": [{"name": "claim_id", "type": "str", "nullable": False}]},
        "queue": {"filter": "ghost == 1"},
    }
    Stage.model_validate(stage)
