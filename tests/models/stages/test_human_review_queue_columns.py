from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage


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
        parse_stage(_queue_stage(
            filter_expr="writer_confirmed == True", edge_columns=["claim_id", "assertion_text"],
        ))


def test_filter_valid_column_clean():
    parse_stage(_queue_stage(
        filter_expr="assertion_text IS NOT NULL", edge_columns=["claim_id", "assertion_text"],
    ))


def test_no_filter_is_clean():
    stage = {
        "id": "wc", "type": "human_review_queue", "name": "wc",
        "inputs": [{"id": "src", "schema": {"columns": [{"name": "claim_id", "type": "str", "nullable": False}]}}],
        "output_schema": {"columns": [{"name": "claim_id", "type": "str", "nullable": False}]},
        "queue": {},
    }
    parse_stage(stage)

