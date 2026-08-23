"""A v1 draft, run through 0005, must satisfy today's Draft model."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from app.models.records.draft import Draft
from conftest import drop_input_schemas
from scripts.stage_signatures import add_signature

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0005_drop_primary_key_from_drafts.py")


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0005", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _v1_draft() -> dict[str, Any]:
    schema = {"columns": [{"name": "id", "type": "str", "nullable": True}],
              "primary_key": ["id"]}
    return {
        "id": "proj/brisk-otter-lamp", "draft_id": "brisk-otter-lamp",
        "stages": [{"id": "tag", "description": "Tag", "type": "python_row_function",
                    "inputs": [{"id": "src", "schema": dict(schema)}],
                    "function": {"kind": "inline", "summary": "Passes rows through.",
                                 "code": "def transform(row):\n    return row"},
                    "output_schema": dict(schema)}],
    }


def test_a_v1_draft_validates_under_todays_model_after_upgrading():
    rev = _load_revision()
    document = _v1_draft()
    assert rev._drop_primary_keys(document) is True
    for stage in document["stages"]:
        add_signature(stage)

    draft = Draft.model_validate({
        **document,
        "stages": [drop_input_schemas(stage) for stage in document["stages"]],
    })

    assert [s.id for s in draft.stages] == ["tag"]
