from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage


def _publish_stage(*, one_file_per, edge_columns):
    return {
        "id": "pub", "type": "publish", "description": "pub",
        "inputs": [{"id": "src", "schema": {
            "columns": [{"name": c, "type": "str", "nullable": False} for c in edge_columns],
        }}],
        "signature": {"form": "replaces"},
        "publish": {"one_file_per": one_file_per},
        "function": {"kind": "inline", "code": "def transform(df, output_dir):\n    return df"},
    }


def test_one_file_per_missing_rejected():
    with pytest.raises(ValidationError):
        parse_stage(_publish_stage(one_file_per="nope", edge_columns=["a"]))


def test_one_file_per_present_ok():
    parse_stage(_publish_stage(one_file_per="a", edge_columns=["a"]))


def test_one_file_per_unset_is_clean():
    parse_stage(_publish_stage(one_file_per=None, edge_columns=["a"]))

