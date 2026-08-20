"""0014 drops a union's `produces`, which only ever restated the shared input schema."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from app.models import parse_stage

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0014_union_signature_extends.py")

# Two columns, as the venezuela_lda_lobbying versions' `headline_figures` union stored them.
_COLUMNS = [{"name": "metric", "type": "str", "nullable": False},
            {"name": "value", "type": "float", "nullable": True}]


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0014", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _union(sid: str = "both") -> dict[str, Any]:
    return {
        "id": sid, "description": "Both halves", "type": "union",
        "inputs": [{"id": "house"}, {"id": "senate"}], "union": {},
        "signature": {"form": "replaces", "reads": [], "produces": _COLUMNS},
    }


def test_a_stored_union_loses_its_produces_and_parses():
    revision = _load_revision()
    document = {"stages": [_union()]}

    assert revision._rewrite_union_signatures(document) is True

    signature = document["stages"][0]["signature"]
    assert signature == {"form": "extends", "reads": [], "adds": [], "rewrites": []}
    assert parse_stage(document["stages"][0]).signature.form == "extends"


def test_every_union_in_one_document_is_rewritten():
    """`any` over a generator short-circuits, which left later unions unmigrated."""
    revision = _load_revision()
    document = {"stages": [_union("first"), _union("second"), _union("third")]}

    revision._rewrite_union_signatures(document)

    assert all(stage["signature"]["form"] == "extends" for stage in document["stages"])


def test_a_document_with_no_union_is_left_alone():
    revision = _load_revision()
    document = {"stages": [{"id": "load", "type": "input_data", "description": "Load"}]}

    assert revision._rewrite_union_signatures(document) is False


def test_a_union_already_migrated_is_not_rewritten_again():
    revision = _load_revision()
    migrated = _union()
    migrated["signature"] = {"form": "extends", "reads": [], "adds": [], "rewrites": []}

    assert revision._rewrite_union_signatures({"stages": [migrated]}) is False
