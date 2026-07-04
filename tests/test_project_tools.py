import json
from pathlib import Path
from typing import Callable

import pytest

from app.chat import project_tools

# Minimal valid handle block per stage type (app/models/stage.py:
# Stage._handle_for_type requires exactly one, keyed by `type`). Mirrors
# tests/test_workspace.py's _HANDLE_BY_TYPE so every fixture stage here
# round-trips through Stage.model_validate rather than landing in `issues`.
_HANDLE_BY_TYPE: dict[str, dict] = {
    "input_data": {"connector": {"kind": "computed_static"}},
    "llm_transform": {"llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {row}"}},
}


def _stage(sid: str, name: str, stype: str, inputs: list[str] | None = None) -> dict:
    stage: dict = {"id": sid, "name": name, "type": stype}
    stage.update(_HANDLE_BY_TYPE.get(stype, {}))
    if inputs:
        stage["inputs"] = [{"id": dep} for dep in inputs]
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


def test_read_tools_report_workspace(tmp_path: Path) -> None:
    _seed(tmp_path, "alpha")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)
    assert _tool(tools, "list_projects")() == ["alpha"]
    assert _tool(tools, "describe_workflow")()["name"] == "alpha"
    assert '"id": "load"' in _tool(tools, "read_stage")("load")


def test_read_stage_missing_fails_loud(tmp_path: Path) -> None:
    _seed(tmp_path, "alpha")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)
    with pytest.raises(ValueError, match="no stage 'nope'"):
        _tool(tools, "read_stage")("nope")
