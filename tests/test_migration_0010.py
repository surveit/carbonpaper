"""0010 gives a stored filter_rows/queue stage the reads 0006 and 0007 left empty."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.models import parse_stage
from scripts import migrate_compiled_stage_files

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0010_backfill_filter_and_queue_reads.py")

_COLUMNS = [{"name": "id", "type": "str", "nullable": True},
            {"name": "score", "type": "int", "nullable": True}]


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0010", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage(stage_type: str, signature: dict[str, Any]) -> dict[str, Any]:
    return {"id": "s", "description": "S", "type": stage_type,
            "inputs": [{"id": "load", "schema": {"columns": _COLUMNS}}],
            "signature": signature}


def test_a_filter_stage_gets_its_whole_anchor_edge():
    rev = _load_revision()
    document = {"stages": [_stage("filter_rows", {"form": "extends"})]}

    assert rev._backfill_document(document) is True

    assert document["stages"][0]["signature"]["reads"] == [
        {"input": "load", "columns": _COLUMNS}]


def test_a_queue_stage_keeps_its_adds_and_gains_reads():
    rev = _load_revision()
    adds = [{"name": "human_score", "type": "int", "nullable": True}]
    document = {"stages": [_stage("human_review_queue", {"form": "extends", "adds": adds})]}

    assert rev._backfill_document(document) is True

    signature = document["stages"][0]["signature"]
    assert signature["adds"] == adds
    assert signature["reads"] == [{"input": "load", "columns": _COLUMNS}]


def test_reads_a_human_already_authored_are_never_widened():
    rev = _load_revision()
    authored = [{"input": "load", "columns": [_COLUMNS[1]]}]
    document = {"stages": [_stage("filter_rows", {"form": "extends", "reads": authored})]}

    assert rev._backfill_document(document) is False
    assert document["stages"][0]["signature"]["reads"] == authored


def test_every_other_stage_type_is_left_alone():
    rev = _load_revision()
    document = {"stages": [_stage("python_row_function", {"form": "extends"})]}

    assert rev._backfill_document(document) is False
    assert "reads" not in document["stages"][0]["signature"]


def test_a_stage_whose_input_declares_no_columns_is_left_for_a_human():
    rev = _load_revision()
    stage = _stage("filter_rows", {"form": "extends"})
    stage["inputs"][0]["schema"] = {"columns": []}
    document = {"stages": [stage]}

    # Inventing an edge here would be fabricating what the stage reads.
    assert rev._backfill_document(document) is False
    assert "reads" not in stage["signature"]


# ── the same rewrite on a project's working copy ─────────────────────────────
def test_a_compiled_filter_file_is_migrated_and_then_parses(tmp_path, monkeypatch):
    compiled = tmp_path / "demo" / "compiled"
    compiled.mkdir(parents=True)
    path = compiled / "keep.json"
    path.write_text(json.dumps({
        **_stage("filter_rows", {"form": "extends"}),
        "filter": {"code": "def should_include(row):\n    return row['score'] > 0\n"},
    }), encoding="utf-8")
    # Before: the file parsed clean under the old model and produced nothing;
    # under today's it does not parse at all.
    with pytest.raises(ValidationError, match="reads nothing"):
        parse_stage(json.loads(path.read_text(encoding="utf-8")))

    monkeypatch.setattr(sys, "argv", [
        "migrate", "--apply", "--projects-dir", str(tmp_path)])
    migrate_compiled_stage_files.main()

    after = parse_stage(json.loads(path.read_text(encoding="utf-8")))
    assert after.anchor_reads() == {"id", "score"}
