"""0011 drops the schema a stored stage kept for each of its inputs."""
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
from scripts.stage_input_schemas import InputRefUnreadable

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0011_drop_stored_input_schemas.py")

_COLUMNS = [{"name": "id", "type": "str", "nullable": True},
            {"name": "score", "type": "int", "nullable": True}]
_SCHEMA = {"columns": _COLUMNS}


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0011", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage(inputs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "tag", "description": "Tag each row.", "type": "python_row_function",
        "inputs": inputs,
        "function": {"kind": "inline", "summary": "Passes rows through.",
                     "code": "def transform(row):\n    return row"},
        "signature": {"form": "extends", "reads": [{"input": "load", "columns": _COLUMNS}]},
    }


def test_a_stage_storing_schema_loses_it_and_then_parses():
    rev = _load_revision()
    document = {"stages": [_stage([{"id": "load", "schema": dict(_SCHEMA)}])]}
    with pytest.raises(ValidationError):
        parse_stage(document["stages"][0])

    assert rev._drop_document_input_schemas(document) is True

    assert document["stages"][0]["inputs"] == [{"id": "load"}]
    assert [i.id for i in parse_stage(document["stages"][0]).inputs] == ["load"]


def test_the_other_spelling_of_the_same_field_goes_too():
    rev = _load_revision()
    # populate_by_name accepted the field name as well as its `schema` alias.
    document = {"stages": [_stage([{"id": "load", "table_schema": dict(_SCHEMA)}])]}

    assert rev._drop_document_input_schemas(document) is True

    assert document["stages"][0]["inputs"] == [{"id": "load"}]


def test_a_document_already_migrated_is_left_untouched():
    rev = _load_revision()
    document = {"stages": [_stage([{"id": "load"}])]}

    assert rev._drop_document_input_schemas(document) is False

    assert document["stages"][0]["inputs"] == [{"id": "load"}]


def test_a_document_with_no_stages_reports_no_change():
    rev = _load_revision()

    assert rev._drop_document_input_schemas({"stages": []}) is False
    assert rev._drop_document_input_schemas({"id": "proj/one"}) is False


def test_an_inputs_payload_of_an_unknown_shape_is_refused_not_guessed():
    rev = _load_revision()
    document = {"stages": [_stage(["load"])]}

    with pytest.raises(InputRefUnreadable, match="not an object"):
        rev._drop_document_input_schemas(document)


# ── the same rewrite on a project's working copy ─────────────────────────────
def test_a_compiled_file_is_migrated_and_then_parses(tmp_path, monkeypatch):
    compiled = tmp_path / "demo" / "compiled"
    compiled.mkdir(parents=True)
    path = compiled / "tag.json"
    path.write_text(json.dumps(_stage([{"id": "load", "schema": dict(_SCHEMA)}])),
                    encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [
        "migrate", "--apply", "--projects-dir", str(tmp_path)])
    migrate_compiled_stage_files.main()

    after = parse_stage(json.loads(path.read_text(encoding="utf-8")))
    assert [i.id for i in after.inputs] == ["load"]


def test_a_compiled_file_that_is_not_json_stops_the_run_before_any_write(tmp_path,
                                                                        monkeypatch):
    compiled = tmp_path / "demo" / "compiled"
    compiled.mkdir(parents=True)
    stale = compiled / "tag.json"
    stale.write_text(json.dumps(_stage([{"id": "load", "schema": dict(_SCHEMA)}])),
                     encoding="utf-8")
    before = stale.read_text(encoding="utf-8")
    (compiled / "torn.json").write_text("{", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [
        "migrate", "--apply", "--projects-dir", str(tmp_path)])
    with pytest.raises(ValueError, match="is not JSON"):
        migrate_compiled_stage_files.main()

    assert stale.read_text(encoding="utf-8") == before
