from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Stage


def _publish_stage(*, one_file_per, edge_columns):
    return {
        "id": "pub", "type": "publish", "name": "pub",
        "inputs": [{"id": "src", "schema": {
            "columns": [{"name": c, "type": "str", "nullable": False} for c in edge_columns],
        }}],
        "output_schema": {"columns": [{"name": "a", "type": "str", "nullable": False}]},
        "publish": {"one_file_per": one_file_per},
        "function": {"kind": "inline", "code": "def transform(df, output_dir):\n    return df"},
    }


def test_one_file_per_missing_rejected():
    with pytest.raises(ValidationError):
        Stage.model_validate(_publish_stage(one_file_per="nope", edge_columns=["a"]))


def test_one_file_per_present_ok():
    Stage.model_validate(_publish_stage(one_file_per="a", edge_columns=["a"]))


def test_one_file_per_unset_is_clean():
    Stage.model_validate(_publish_stage(one_file_per=None, edge_columns=["a"]))

