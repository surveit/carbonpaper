"""The 12 editing tools wrapped as an in-process claude_agent_sdk MCP
server. These tests reach the wrapped `SdkMcpTool` handlers directly (no CLI
subprocess) to prove the adapter forwards results and surfaces errors loudly.

The example workspace (`examples/`) is gitignored and absent in a fresh
worktree, so — like tests/test_project_tools.py — we seed a project into
tmp_path (and point the service surface at it) instead of depending on a
checked-in `examples/congresswatch`.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool
from pydantic import BaseModel

from app.agents.compiler.tools import (
    TOOL_SCHEMAS,
    EditingContext,
    make_editing_tools,
)
from app.core.agent.registry import _as_content, build_mcp_server
from app.services import workspace


@pytest.fixture(autouse=True)
def examples_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    return tmp_path


def _build(name: str) -> tuple[Any, list[str], list[SdkMcpTool[Any]]]:
    return build_mcp_server(make_editing_tools(EditingContext(project_id=name)), TOOL_SCHEMAS)


def _call(tool: SdkMcpTool[Any], args: dict[str, Any]) -> dict[str, Any]:
    """Drive one tool handler to completion without a CLI subprocess."""

    async def _run() -> dict[str, Any]:
        return await tool.handler(args)

    return asyncio.run(_run())


def _seed(examples: Path, name: str) -> Path:
    """Write one minimal, valid stage so read_stage/describe_workflow have real
    on-disk state (mirrors tests/test_project_tools.py::_seed)."""
    compiled = examples / name / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    stage = {
        "id": "load",
        "name": "Load rows",
        "type": "input_data",
        "connector": {"kind": "file"},
    }
    (compiled / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")
    return examples / name


def test_allowed_names_cover_every_tool(examples_root: Path) -> None:
    _seed(examples_root, "congresswatch")
    _server, allowed, _tools = _build("congresswatch")
    assert set(allowed) == {f"mcp__tools__{n}" for n in TOOL_SCHEMAS}
    assert len(allowed) == 12


def test_read_stage_handler_returns_text_content(examples_root: Path) -> None:
    pdir = _seed(examples_root, "congresswatch")
    _server, _allowed, tools = _build("congresswatch")
    tool = next(t for t in tools if t.name == "read_stage")  # SdkMcpTool

    from app.services.workspace import project_workflow_summary

    stage_id = project_workflow_summary(pdir)["stages"][0]["id"]
    out = _call(tool, {"project_id": "congresswatch", "stage_id": stage_id})
    assert out["content"][0]["type"] == "text"
    assert stage_id in out["content"][0]["text"]


def test_handler_surfaces_tool_error_not_fabricated_value(examples_root: Path) -> None:
    _seed(examples_root, "congresswatch")
    _server, _allowed, tools = _build("congresswatch")
    tool = next(t for t in tools if t.name == "read_stage")
    out = _call(tool, {"project_id": "congresswatch", "stage_id": "no_such_stage"})
    assert out.get("is_error") is True
    assert "no_such_stage" in out["content"][0]["text"]


def test_draft_round_trip_creates_an_unpublished_version(examples_root: Path) -> None:
    _server, _allowed, tools = _build("congresswatch")
    by_name = {t.name: t for t in tools}
    stage = {
        "id": "load",
        "name": "Load rows",
        "type": "input_data",
        "connector": {"kind": "file"},
    }

    created = _call(by_name["create_draft"], {"project_id": "congresswatch"})
    draft = json.loads(created["content"][0]["text"])
    assert len(draft["id"].split("-")) == 3
    assert draft["stages"] == []

    edited = _call(
        by_name["set_draft_stage"],
        {
            "project_id": "congresswatch",
            "draft_id": draft["id"],
            "stage_json": json.dumps(stage),
        },
    )
    edit_result = json.loads(edited["content"][0]["text"])
    assert edit_result["ok"] is True
    assert edit_result["stage_ids"] == ["load"]
    assert edit_result["issues"] == []

    # read_draft round-trips the stage as a full Stage dump (unset optionals
    # come back as explicit nulls — registry._as_content doesn't exclude_none)
    # but every field the agent WROTE survives unchanged, in alias form.
    read_back = _call(
        by_name["read_draft"],
        {"project_id": "congresswatch", "draft_id": draft["id"]},
    )
    read_result = json.loads(read_back["content"][0]["text"])
    assert len(read_result["stages"]) == 1
    read_stage = read_result["stages"][0]
    assert read_stage["id"] == stage["id"]
    assert read_stage["name"] == stage["name"]
    assert read_stage["type"] == stage["type"]
    assert read_stage["connector"]["kind"] == stage["connector"]["kind"]

    saved = _call(
        by_name["save_version"],
        {
            "project_id": "congresswatch",
            "draft_id": draft["id"],
            "message": "add the load stage",
        },
    )
    save_result = json.loads(saved["content"][0]["text"])
    assert save_result["ok"] is True
    assert save_result["version"]["published"] is False


def test_set_draft_stage_rejects_malformed_stage_as_tool_error(examples_root: Path) -> None:
    """A malformed stage passed through the MCP tool boundary surfaces as an
    is_error tool result (the ValueError set_draft_stage raises propagates
    through registry._wrap), not a stored stage with issues."""
    _server, _allowed, tools = _build("congresswatch")
    by_name = {t.name: t for t in tools}

    created = _call(by_name["create_draft"], {"project_id": "congresswatch"})
    draft = json.loads(created["content"][0]["text"])

    malformed = {"id": "load", "type": "input_data"}  # missing name + connector
    out = _call(
        by_name["set_draft_stage"],
        {
            "project_id": "congresswatch",
            "draft_id": draft["id"],
            "stage_json": json.dumps(malformed),
        },
    )
    assert out.get("is_error") is True

    read_back = _call(
        by_name["read_draft"],
        {"project_id": "congresswatch", "draft_id": draft["id"]},
    )
    read_result = json.loads(read_back["content"][0]["text"])
    assert read_result["stages"] == []


def test_draft_stage_input_schema_round_trips_in_alias_form(examples_root: Path) -> None:
    """A stage's `inputs[].schema` — Pydantic's InputRef.table_schema field,
    aliased to the wire name `schema` — must come back to the agent under
    `schema`, the same key the agent wrote, never the python field name
    `table_schema`. Exercises registry._as_content's by_alias=True dump for
    the one aliased field on Stage."""
    _server, _allowed, tools = _build("congresswatch")
    by_name = {t.name: t for t in tools}
    upstream_schema = {"columns": [{"name": "id", "type": "str"}], "primary_key": ["id"]}
    downstream = {
        "id": "transform",
        "name": "Transform rows",
        "type": "python_row_function",
        "inputs": [{"id": "load", "schema": upstream_schema}],
        "function": {"kind": "inline", "code": "def transform(row): return row"},
    }

    created = _call(by_name["create_draft"], {"project_id": "congresswatch"})
    draft = json.loads(created["content"][0]["text"])
    _call(
        by_name["set_draft_stage"],
        {
            "project_id": "congresswatch",
            "draft_id": draft["id"],
            "stage_json": json.dumps(downstream),
        },
    )

    read_back = _call(
        by_name["read_draft"],
        {"project_id": "congresswatch", "draft_id": draft["id"]},
    )
    read_result = json.loads(read_back["content"][0]["text"])
    stage = next(s for s in read_result["stages"] if s["id"] == "transform")
    assert "schema" in stage["inputs"][0]
    assert "table_schema" not in stage["inputs"][0]
    assert stage["inputs"][0]["schema"]["primary_key"] == ["id"]


def test_unknown_draft_id_surfaces_as_tool_error(examples_root: Path) -> None:
    _server, _allowed, tools = _build("congresswatch")
    tool = next(t for t in tools if t.name == "read_draft")
    out = _call(tool, {"project_id": "congresswatch", "draft_id": "calm-otter-lamp"})
    assert out.get("is_error") is True
    assert "No draft" in out["content"][0]["text"]


def test_as_content_serializes_a_pydantic_model_to_its_fields() -> None:
    """A tool returning a pydantic model (e.g. the draft tools' DraftView /
    SaveResult) must reach the model as JSON content, not an unserializable
    object — this is the seam create_draft/read_draft/set_draft_stage/
    remove_draft_stage/save_version all rely on."""

    class _Sample(BaseModel):
        ok: bool
        label: str

    out = _as_content(_Sample(ok=True, label="draft"))
    assert json.loads(out["content"][0]["text"]) == {"ok": True, "label": "draft"}
