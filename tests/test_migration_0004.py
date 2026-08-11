"""A v1 document, run through 0004, must satisfy today's model.

Nothing repairs `primary_key` at read time any more, so this revision is the only
thing standing between a v1 stage spec and a store that no longer loads.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from app.models.workflow import parse_workflow
from scripts.stage_signatures import add_signature

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0004_drop_primary_key_from_stage_schemas.py")


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0004", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _column(name: str) -> dict[str, Any]:
    return {"name": name, "type": "str", "nullable": True}


def _v1_stages() -> list[dict[str, Any]]:
    schema = {"columns": [_column("id")], "primary_key": ["id"]}
    return [
        {"id": "src", "description": "Source", "type": "input_data",
         "connector": {"kind": "file", "params": {"format": "csv", "path": "/tmp/a.csv"}},
         "inputs": [], "output_schema": dict(schema)},
        {"id": "tag", "description": "Tag", "type": "python_row_function",
         "inputs": [{"id": "src", "schema": dict(schema)}],
         "function": {"kind": "inline", "summary": "Passes rows through.",
                      "code": "def transform(row):\n    return row"},
         "output_schema": dict(schema)},
    ]


def test_a_v1_document_validates_under_todays_model_after_upgrading():
    rev = _load_revision()
    document = {"stages": _v1_stages()}
    assert rev._drop_primary_keys(document) is True
    # 0006 carries the same document the rest of the way, as a store crossing
    # both revisions would be.
    for stage in document["stages"]:
        add_signature(stage)

    parse_workflow(document["stages"])  # raises if the upgraded shape is still invalid

    assert "primary_key" not in document["stages"][1]["inputs"][0]["schema"]


def test_a_document_with_no_key_left_is_reported_unchanged():
    rev = _load_revision()
    document = {"stages": _v1_stages()}
    rev._drop_primary_keys(document)
    assert rev._drop_primary_keys(document) is False
