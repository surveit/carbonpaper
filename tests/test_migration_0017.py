"""0017 renames the `publish` stage type, and its config block, to `report`."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from app.models import parse_stage

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0017_publish_stage_becomes_report.py")

_COLUMNS = [{"name": "filing_id", "type": "str", "nullable": False}]


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0017", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stored_publish(sid: str = "publish_evidence_table") -> dict[str, Any]:
    return {
        "id": sid, "description": "Write the evidence table.", "type": "publish",
        "inputs": [{"id": "capture_evidence"}],
        "publish": {"format": "csv", "destination": "build/"},
        "function": {
            "kind": "inline", "summary": "Writes the evidence table as CSV.",
            "code": ("def transform(df, output_dir):\n"
                     "    df.to_csv(f'{output_dir}/evidence.csv', index=False)\n"
                     "    return df"),
        },
        "signature": {"form": "replaces",
                      "reads": [{"input": "capture_evidence", "columns": _COLUMNS}]},
    }


def test_a_stored_publish_stage_is_renamed_and_then_parses():
    revision = _load_revision()
    document = {"stages": [_stored_publish()]}

    assert revision._rename_stage_specs(document, "publish", "report") is True

    stage = document["stages"][0]
    assert stage["type"] == "report"
    assert stage["report"] == {"format": "csv", "destination": "build/"}
    assert "publish" not in stage
    assert parse_stage(stage).report.destination == "build/"


def test_every_publish_stage_in_one_document_is_renamed():
    """`any` over a generator short-circuits, which left later stages unrenamed."""
    revision = _load_revision()
    document = {"stages": [_stored_publish("first"), _stored_publish("second")]}

    revision._rename_stage_specs(document, "publish", "report")

    assert [s["type"] for s in document["stages"]] == ["report", "report"]


def test_a_document_with_no_publish_stage_is_left_alone():
    revision = _load_revision()
    document = {"stages": [{"id": "load", "type": "input_data", "description": "Load"}]}

    assert revision._rename_stage_specs(document, "publish", "report") is False


def test_a_stage_already_renamed_is_not_renamed_again():
    revision = _load_revision()
    document = {"stages": [_stored_publish()]}
    revision._rename_stage_specs(document, "publish", "report")

    assert revision._rename_stage_specs(document, "publish", "report") is False


def test_a_run_record_names_the_type_it_executed():
    revision = _load_revision()
    manifest = {"stage_records": [{"stage_id": "load", "type": "input_data"},
                                  {"stage_id": "pub", "type": "publish"}]}

    assert revision._rename_stage_records(manifest, "publish", "report") is True

    assert [r["type"] for r in manifest["stage_records"]] == ["input_data", "report"]


def test_a_run_record_keeps_no_config_block_of_its_own():
    revision = _load_revision()
    manifest = {"stage_records": [{"stage_id": "pub", "type": "publish"}]}

    revision._rename_stage_records(manifest, "publish", "report")

    assert set(manifest["stage_records"][0]) == {"stage_id", "type"}


def test_the_rename_reverses():
    revision = _load_revision()
    document = {"stages": [_stored_publish()]}
    revision._rename_stage_specs(document, "publish", "report")

    assert revision._rename_stage_specs(document, "report", "publish") is True

    assert document["stages"][0] == _stored_publish()
