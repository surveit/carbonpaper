import json
from pathlib import Path
from typing import Callable

import pytest

from app.agents.compiler.tools import EditingContext, make_editing_tools
from app.services import workspace

# Minimal valid handle block per stage type (app/models/stage.py:
# Stage._handle_for_type requires exactly one, keyed by `type`). Mirrors
# tests/test_workspace.py's _HANDLE_BY_TYPE so every fixture stage here
# round-trips through Stage.model_validate rather than landing in `issues`.
_HANDLE_BY_TYPE: dict[str, dict] = {
    "input_data": {"connector": {"kind": "file"}},
    "llm_transform": {"llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {row}"}},
}

# Stage now requires a schema on every input and (outside `publish`) an
# output_schema. The only producer these fixtures build is `load`, so an input
# edge always carries `load`'s output_schema; the llm_transform's output adds
# `score` on top of it, keeping the same primary_key so it stays strictly 1:1.
_LOAD_COLUMNS: list[dict] = [
    {"name": "id", "type": "str", "nullable": False},
    {"name": "row", "type": "str", "nullable": False},
]
_LOAD_SCHEMA: dict = {"columns": _LOAD_COLUMNS, "primary_key": ["id"]}
_SCORE_SCHEMA: dict = {
    "columns": [*_LOAD_COLUMNS, {"name": "score", "type": "float", "nullable": True}],
    "primary_key": ["id"],
}
_OUTPUT_SCHEMA_BY_TYPE: dict[str, dict] = {
    "input_data": _LOAD_SCHEMA,
    "llm_transform": _SCORE_SCHEMA,
}


@pytest.fixture(autouse=True)
def examples_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the name-based service surface at a tmp examples root, so the tools —
    which resolve names against workspace.EXAMPLES_DIR internally — read and
    write there rather than the real workspace."""
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    return tmp_path


def _tools(name: str) -> list[Callable]:
    return make_editing_tools(EditingContext(project_id=name))


def _stage(sid: str, name: str, stype: str, inputs: list[str] | None = None) -> dict:
    stage: dict = {"id": sid, "name": name, "type": stype}
    stage.update(_HANDLE_BY_TYPE.get(stype, {}))
    if stype in _OUTPUT_SCHEMA_BY_TYPE:
        stage["output_schema"] = _OUTPUT_SCHEMA_BY_TYPE[stype]
    if inputs:
        stage["inputs"] = [{"id": dep, "schema": _LOAD_SCHEMA} for dep in inputs]
    return stage


def _seed(examples: Path, name: str) -> Path:
    compiled = examples / name / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "01_load.json").write_text(
        json.dumps(_stage("load", "Load rows", "input_data")), encoding="utf-8"
    )
    return examples / name


def _tool(tools: list[Callable], fn_name: str) -> Callable:
    for tool in tools:
        if tool.__name__ == fn_name:
            return tool
    raise AssertionError(f"tool {fn_name!r} not registered")


def test_read_tools_report_workspace(examples_root: Path) -> None:
    _seed(examples_root, "alpha")
    tools = _tools("alpha")
    assert _tool(tools, "list_projects")() == ["alpha"]
    assert _tool(tools, "get_current_project")() == "alpha"
    assert _tool(tools, "describe_workflow")("alpha")["name"] == "alpha"
    assert '"id": "load"' in _tool(tools, "read_stage")("alpha", "load")


def test_read_stage_missing_fails_loud(examples_root: Path) -> None:
    _seed(examples_root, "alpha")
    tools = _tools("alpha")
    with pytest.raises(ValueError, match="no stage 'nope'"):
        _tool(tools, "read_stage")("alpha", "nope")


def test_edit_stage_tool_writes_and_reports_ok(examples_root: Path) -> None:
    pdir = _seed(examples_root, "alpha")
    tools = _tools("alpha")
    out = _tool(tools, "edit_stage")(
        "alpha", "load", json.dumps(_stage("load", "Load rows v2", "input_data"))
    )
    # The tool reports only ok + issues; the node's review colour is derived by the
    # review layer, not returned by the writer.
    assert out["ok"] is True and out == {"ok": True, "issues": []}
    assert "Load rows v2" in (pdir / "compiled" / "01_load.json").read_text(encoding="utf-8")


def test_edit_stage_tool_invalid_writes_nothing_and_reports_issues(examples_root: Path) -> None:
    pdir = _seed(examples_root, "alpha")
    before = (pdir / "compiled" / "01_load.json").read_text(encoding="utf-8")
    tools = _tools("alpha")
    out = _tool(tools, "edit_stage")(
        "alpha", "load", json.dumps({"id": "load", "name": "x", "type": "not_a_real_type"})
    )
    assert out["ok"] is False and out["issues"]
    assert (pdir / "compiled" / "01_load.json").read_text(encoding="utf-8") == before


def test_project_id_cannot_escape_the_workspace(examples_root: Path) -> None:
    _seed(examples_root, "alpha")
    tools = _tools("alpha")
    with pytest.raises(ValueError, match="invalid project id"):
        _tool(tools, "describe_workflow")("../outside")
