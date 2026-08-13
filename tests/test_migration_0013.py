"""0013 moves a module-kind stage's code onto the stage, or refuses the document."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from app.models import parse_stage
from scripts.module_function_code import (
    ModuleSourceUnreadable,
    inline_module_function,
    resolve_module_path,
)

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0013_inline_module_function_code.py")

# The shape the one real module-kind stage held: dsa_evidence_capture's
# capture_comment_evidence, whose Playwright capture lived in the project's own
# code/ directory and stopped importing when the store left the checkout.
_MODULE = "examples.dsa_evidence_capture.code.capture"
_SOURCE = "def transform(row: dict) -> dict:\n    return {**row, 'captured': True}\n"
_COLUMNS = [{"name": "post_url", "type": "str", "nullable": False}]


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0013", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _projects_root(tmp_path: Path, source: str = _SOURCE) -> Path:
    root = tmp_path / "examples"
    code_dir = root / "dsa_evidence_capture" / "code"
    code_dir.mkdir(parents=True)
    (code_dir / "capture.py").write_text(source, encoding="utf-8")
    return root


def _stage(function: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "capture_comment_evidence",
        "description": "Capture live evidence per comment.",
        "type": "python_row_function",
        "inputs": [{"id": "prepare_capture_targets"}],
        "function": function,
        "signature": {"form": "extends",
                      "reads": [{"input": "prepare_capture_targets", "columns": _COLUMNS}],
                      "adds": [{"name": "captured", "type": "bool", "nullable": False}]},
    }


def _module_function() -> dict[str, Any]:
    return {"kind": "module", "module": _MODULE, "function": "transform",
            "requirements": ["playwright>=1.40"]}


def test_the_module_source_lands_in_code_and_the_stage_then_parses(tmp_path):
    stage = _stage(_module_function())
    with pytest.raises(Exception):
        parse_stage(stage)

    assert inline_module_function(stage, _projects_root(tmp_path)) is True

    assert stage["function"]["kind"] == "inline"
    assert stage["function"]["code"] == _SOURCE
    assert "module" not in stage["function"]
    assert parse_stage(stage).function.code == _SOURCE


def test_what_the_module_did_not_name_is_left_alone(tmp_path):
    stage = _stage(_module_function())

    inline_module_function(stage, _projects_root(tmp_path))

    assert stage["function"]["function"] == "transform"
    assert stage["function"]["requirements"] == ["playwright>=1.40"]


def test_an_absent_source_file_stops_the_migration_rather_than_storing_nothing(tmp_path):
    stage = _stage(_module_function())
    empty_root = tmp_path / "examples"
    empty_root.mkdir()

    with pytest.raises(ModuleSourceUnreadable, match="no source file"):
        inline_module_function(stage, empty_root)

    assert stage["function"]["kind"] == "module"


def test_a_source_file_holding_nothing_is_refused_not_written(tmp_path):
    stage = _stage(_module_function())

    with pytest.raises(ModuleSourceUnreadable, match="holds no code"):
        inline_module_function(stage, _projects_root(tmp_path, source="\n\n"))


def test_a_module_binding_no_transform_is_refused(tmp_path):
    stage = _stage(_module_function())
    root = _projects_root(tmp_path, source="CAPTURED = True\n")

    with pytest.raises(ValueError, match="must define"):
        inline_module_function(stage, root)


def test_a_module_outside_the_projects_root_is_refused_not_guessed_at(tmp_path):
    stage = _stage({"kind": "module", "module": "somepackage.capture"})

    with pytest.raises(ModuleSourceUnreadable, match="does not begin with"):
        inline_module_function(stage, _projects_root(tmp_path))


def test_a_module_kind_carrying_no_module_is_refused(tmp_path):
    stage = _stage({"kind": "module"})

    with pytest.raises(ModuleSourceUnreadable, match="carries no `module`"):
        inline_module_function(stage, _projects_root(tmp_path))


def test_a_stage_holding_both_module_and_code_is_a_human_decision(tmp_path):
    stage = _stage({**_module_function(), "code": "def transform(row):\n    return row"})

    with pytest.raises(ModuleSourceUnreadable, match="must say which one"):
        inline_module_function(stage, _projects_root(tmp_path))


def test_an_inline_stage_is_left_untouched(tmp_path):
    function = {"kind": "inline", "code": _SOURCE}
    stage = _stage(dict(function))

    assert inline_module_function(stage, _projects_root(tmp_path)) is False

    assert stage["function"] == function


def test_running_it_twice_changes_nothing_the_second_time(tmp_path):
    root = _projects_root(tmp_path)
    stage = _stage(_module_function())

    assert inline_module_function(stage, root) is True
    assert inline_module_function(stage, root) is False


def test_a_document_with_no_stages_reports_no_change(tmp_path):
    rev = _load_revision()
    root = _projects_root(tmp_path)

    assert rev._inline_document_modules({"stages": []}, root) is False
    assert rev._inline_document_modules({"id": "proj/one"}, root) is False


def test_a_document_carrying_one_module_stage_is_rewritten(tmp_path):
    rev = _load_revision()
    document = {"stages": [_stage({"kind": "inline", "code": _SOURCE}),
                           _stage(_module_function())]}

    assert rev._inline_document_modules(document, _projects_root(tmp_path)) is True

    assert [s["function"]["kind"] for s in document["stages"]] == ["inline", "inline"]


@pytest.mark.parametrize(("module", "expected"), [
    (_MODULE, "dsa_evidence_capture/code/capture.py"),
    ("examples.proj.code.build", "proj/code/build.py"),
])
def test_the_leading_segment_names_the_projects_root(module, expected, tmp_path):
    root = tmp_path / "examples"

    assert resolve_module_path(module, "s", root) == root / expected
