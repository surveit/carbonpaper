"""0013 names the model an `llm_transform` stored before the rule was running on."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.models import parse_stage
from app.runtime.options import DEFAULT_MODEL
from app.services import workspace
from scripts import migrate_compiled_stage_files
from scripts.llm_model import LlmConfigUnreadable

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0013_require_llm_model.py")

_COLUMNS = [{"name": "text", "type": "str", "nullable": True}]


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0013", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _judge_stage(llm: Any) -> dict[str, Any]:
    return {
        "id": "judge_alignment", "description": "Judge the alignment.",
        "type": "llm_transform", "inputs": [{"id": "matched"}],
        "llm": llm,
        "signature": {
            "form": "extends",
            "reads": [{"input": "matched", "columns": _COLUMNS}],
            "adds": [{"name": "verdict", "type": "str", "nullable": True}],
        },
    }


def test_a_stage_naming_no_model_is_given_the_one_it_ran_on_and_then_parses():
    rev = _load_revision()
    document = {"stages": [_judge_stage({"prompt_data_template": "{text}"})]}
    with pytest.raises(ValidationError):
        parse_stage(document["stages"][0])

    assert rev._stamp_document(document) is True

    stage = document["stages"][0]
    assert stage["llm"]["model"] == DEFAULT_MODEL.value
    assert parse_stage(stage).llm.model == DEFAULT_MODEL


def test_a_stage_that_already_names_a_model_keeps_it():
    rev = _load_revision()
    document = {"stages": [_judge_stage(
        {"prompt_data_template": "{text}", "model": "claude-opus-5"})]}

    assert rev._stamp_document(document) is False

    assert document["stages"][0]["llm"]["model"] == "claude-opus-5"


def test_a_stage_of_another_type_is_left_alone():
    rev = _load_revision()
    document = {"stages": [{"id": "load", "type": "input_data"}]}

    assert rev._stamp_document(document) is False

    assert "llm" not in document["stages"][0]


def test_a_document_with_no_stages_reports_no_change():
    rev = _load_revision()

    assert rev._stamp_document({"stages": []}) is False
    assert rev._stamp_document({"id": "proj/one"}) is False


def test_an_llm_payload_of_an_unknown_shape_is_refused_not_guessed():
    rev = _load_revision()
    document = {"stages": [_judge_stage("{text}")]}

    with pytest.raises(LlmConfigUnreadable, match="not an object"):
        rev._stamp_document(document)


# ── the same rewrite on a project's working copy ─────────────────────────────
def _write_project(root: Path) -> Path:
    compiled = root / "demo" / "compiled"
    compiled.mkdir(parents=True)
    path = compiled / "05_judge_alignment.json"
    path.write_text(json.dumps(_judge_stage({"prompt_data_template": "{text}"})),
                    encoding="utf-8")
    return path


def test_a_compiled_file_is_stamped_and_then_parses(tmp_path, monkeypatch):
    path = _write_project(tmp_path)

    monkeypatch.setattr(sys, "argv", [
        "migrate", "--apply", "--projects-dir", str(tmp_path)])
    migrate_compiled_stage_files.main()

    spec = json.loads(path.read_text(encoding="utf-8"))
    assert spec["llm"]["model"] == DEFAULT_MODEL.value
    parse_stage(spec)


def test_the_revision_itself_reaches_the_working_copy_not_only_the_store(tmp_path, monkeypatch):
    """The half missed by 0004–0012, which is what left stored projects unable to load."""
    path = _write_project(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(workspace, "_projects_dir", None)

    _load_revision()._stamp_compiled_working_copies()

    assert json.loads(path.read_text(encoding="utf-8"))["llm"]["model"] == DEFAULT_MODEL.value


def test_a_missing_projects_root_is_not_a_failed_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_PROJECTS_DIR", str(tmp_path / "nothing-here"))
    monkeypatch.setattr(workspace, "_projects_dir", None)

    _load_revision()._stamp_compiled_working_copies()
