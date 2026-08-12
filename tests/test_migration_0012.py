"""0012 retires a publish stage's `template`, keeping what it said as a note."""
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
from scripts.publish_template import NOTE_PREFIX, PublishTemplateUnreadable

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0012_retire_publish_template.py")

# The real template from hate_on_activist_pages' evidence-table stage: prose
# naming the columns, which is why the field's content is kept rather than cut.
_TEMPLATE = ("platform · page · post_url · document_id · timestamp · comment_text "
             "(original) · translation_en")
_COLUMNS = [{"name": "document_id", "type": "str", "nullable": False}]


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0012", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage(publish: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "publish_evidence_table", "description": "Write the evidence table.",
        "type": "publish", "inputs": [{"id": "capture_evidence"}],
        "publish": publish,
        "function": {
            "kind": "inline", "summary": "Writes the evidence table as CSV.",
            "code": ("def transform(df, output_dir):\n"
                     "    path = f'{output_dir}/evidence.csv'\n"
                     "    df.to_csv(path, index=False)\n"
                     "    return [path]"),
        },
        "signature": {"form": "replaces",
                      "reads": [{"input": "capture_evidence", "columns": _COLUMNS}]},
    }


def test_a_stage_storing_a_template_loses_the_field_and_then_parses():
    rev = _load_revision()
    document = {"stages": [_stage({"format": "csv", "template": _TEMPLATE})]}
    with pytest.raises(ValidationError):
        parse_stage(document["stages"][0])

    assert rev._retire_document_templates(document) is True

    stage = document["stages"][0]
    assert stage["publish"] == {"format": "csv"}
    assert parse_stage(stage).publish.format is not None


def test_what_the_template_said_is_kept_as_a_note():
    rev = _load_revision()
    document = {"stages": [_stage({"format": "csv", "template": _TEMPLATE})]}

    rev._retire_document_templates(document)

    assert document["stages"][0]["compiler_notes"] == [f"{NOTE_PREFIX}{_TEMPLATE}"]


def test_an_empty_template_leaves_no_note_behind():
    rev = _load_revision()
    document = {"stages": [_stage({"format": "csv", "template": ""})]}

    assert rev._retire_document_templates(document) is True

    assert document["stages"][0].get("compiler_notes", []) == []


def test_a_document_already_migrated_is_left_untouched():
    rev = _load_revision()
    document = {"stages": [_stage({"format": "csv"})]}

    assert rev._retire_document_templates(document) is False

    assert document["stages"][0]["publish"] == {"format": "csv"}


def test_running_it_twice_does_not_double_the_note():
    rev = _load_revision()
    document = {"stages": [_stage({"format": "csv", "template": _TEMPLATE})]}

    rev._retire_document_templates(document)
    assert rev._retire_document_templates(document) is False

    assert document["stages"][0]["compiler_notes"] == [f"{NOTE_PREFIX}{_TEMPLATE}"]


def test_a_document_with_no_stages_reports_no_change():
    rev = _load_revision()

    assert rev._retire_document_templates({"stages": []}) is False
    assert rev._retire_document_templates({"id": "proj/one"}) is False


def test_a_publish_payload_of_an_unknown_shape_is_refused_not_guessed():
    rev = _load_revision()
    document = {"stages": [_stage(["csv"])]}  # type: ignore[list-item]

    with pytest.raises(PublishTemplateUnreadable, match="not an object"):
        rev._retire_document_templates(document)


# ── the same rewrite on a project's working copy ─────────────────────────────
def test_a_compiled_file_is_migrated_and_then_parses(tmp_path, monkeypatch):
    compiled = tmp_path / "demo" / "compiled"
    compiled.mkdir(parents=True)
    path = compiled / "publish_evidence_table.json"
    path.write_text(json.dumps(_stage({"format": "csv", "template": _TEMPLATE})),
                    encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [
        "migrate", "--apply", "--projects-dir", str(tmp_path)])
    migrate_compiled_stage_files.main()

    spec = json.loads(path.read_text(encoding="utf-8"))
    assert "template" not in spec["publish"]
    assert spec["compiler_notes"] == [f"{NOTE_PREFIX}{_TEMPLATE}"]
    parse_stage(spec)
