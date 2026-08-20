"""`examples/` is gitignored and absent in a fresh worktree, so a project is seeded
into tmp_path rather than read from a checked-in example.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool
from pydantic import BaseModel

from app.tools.editing import EditingContext, build_editing_tools
from app.core.agent.registry import as_tool_content, build_mcp_server
from app.core.agent.bound_tool import bind_by_signature
from app.tools import shared
from app.tools.tool_specs import bind
from app.services import workspace
from stage_seed import add_stage


@pytest.fixture(autouse=True)
def examples_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _build(name: str) -> tuple[Any, list[str], list[SdkMcpTool[Any]]]:
    return build_mcp_server(build_editing_tools(EditingContext(project_id=name, base_url="http://reader.test/")))


def _call(tool: SdkMcpTool[Any], args: dict[str, Any]) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        return await tool.handler(args)

    return asyncio.run(_run())


def _seed(examples: Path, name: str) -> Path:
    compiled = examples / name
    compiled.mkdir(parents=True, exist_ok=True)
    stage = {
        "id": "load",
        "description": "Load rows",
        "type": "input_data",
        "connector": {"kind": "file"},
        "signature": {
            "form": "replaces",
            "produces": [{"name": "id", "type": "str", "nullable": False}],
        },
    }
    add_stage(compiled, stage)
    return examples / name


def test_allowed_names_cover_every_tool(examples_root: Path) -> None:
    _seed(examples_root, "congresswatch")
    _server, allowed, _tools = _build("congresswatch")
    specs = build_editing_tools(EditingContext(project_id="congresswatch", base_url="http://reader.test/"))
    assert set(allowed) == {f"mcp__tools__{spec.name}" for spec in specs}
    assert len(allowed) == 28


def test_read_stage_handler_returns_text_content(examples_root: Path) -> None:
    pdir = _seed(examples_root, "congresswatch")
    _server, _allowed, tools = _build("congresswatch")
    tool = next(t for t in tools if t.name == "read_stage")  # SdkMcpTool

    from app.services.workspace import project_workflow_summary

    stage_id = project_workflow_summary(pdir.name).stages[0].id
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


def test_add_stage_then_save_creates_an_unpublished_version(examples_root: Path) -> None:
    _seed(examples_root, "congresswatch")
    _server, _allowed, tools = _build("congresswatch")
    by_name = {t.name: t for t in tools}
    stage = {
        "id": "score",
        "description": "Score rows",
        "type": "starlark_row_function",
        "inputs": [{"id": "load"}],
        "signature": {"form": "extends",
                      "reads": [{"input": "load", "columns": [
                          {"name": "id", "type": "str", "nullable": False}]}],
                      "adds": [{"name": "score", "type": "float", "nullable": True}]},
        "starlark": {
            "summary": "Scores every row 1.0, which is what a fixture needs and no more.",
            "corner_cases": [],
            "code": "def transform(row):\n    return {'score': 1.0}\n",
        },
    }

    out = _call(by_name["add_stage"], {"project_id": "congresswatch", "stages": [stage]})
    assert not out.get("is_error"), out["content"][0]["text"]
    assert json.loads(out["content"][0]["text"])["added"] == ["score"], out["content"][0]["text"]

    read_back = json.loads(_call(
        by_name["read_stage"],
        {"project_id": "congresswatch", "stage_id": "score"})["content"][0]["text"])
    assert read_back["id"] == stage["id"]
    assert read_back["type"] == stage["type"]

    saved = json.loads(_call(by_name["save_version"], {
        "project_id": "congresswatch",
        "message": "add the score stage"})["content"][0]["text"])
    assert saved["ok"] is True
    assert saved["version_id"] is not None





def test_as_content_serializes_a_pydantic_model_to_its_fields() -> None:
    class _Sample(BaseModel):
        ok: bool
        label: str

    out = as_tool_content(_Sample(ok=True, label="draft"))
    assert json.loads(out["content"][0]["text"]) == {"ok": True, "label": "draft"}


def test_a_tool_taking_a_model_is_handed_json_and_gets_the_model(examples_root: Path) -> None:
    """add_stage and write_review_guide declare pydantic models; the SDK sends dicts."""
    _seed(examples_root, "congresswatch")
    _server, _allowed, tools = _build("congresswatch")
    spec = next(s for s in build_editing_tools(EditingContext(project_id="congresswatch", base_url="http://reader.test/"))
                if s.name == "add_stage")

    parsed = spec.parse_arguments({
        "project_id": "congresswatch",
        "stages": [{
            "id": "load", "description": "Load rows", "type": "input_data",
            "connector": {"kind": "file"},
            "signature": {"form": "replaces", "produces": [
                {"name": "id", "type": "str", "nullable": False}]},
        }],
    })
    assert parsed["stages"][0].id == "load"
    assert parsed["project_id"] == "congresswatch"


def test_an_argument_the_model_shapes_wrongly_comes_back_as_a_tool_error(
    examples_root: Path,
) -> None:
    _seed(examples_root, "congresswatch")
    _server, _allowed, tools = _build("congresswatch")
    out = _call(next(t for t in tools if t.name == "add_stage"),
                {"project_id": "congresswatch", "stages": [{"id": "load"}]})
    assert out["is_error"] is True
    # The field, not a stack trace: what comes back is what the model reads to retry.
    assert "type" in out["content"][0]["text"]


def test_a_model_parameter_is_advertised_with_its_own_shape(examples_root: Path) -> None:
    """The SDK maps a type it does not know to a bare string, and a string has no fields."""
    _seed(examples_root, "congresswatch")
    by_name = {s.name: s for s in build_editing_tools(
        EditingContext(project_id="congresswatch", base_url="http://reader.test/"))}

    schema = by_name["write_review_guide"].json_schema
    guide = schema["properties"]["guide"]
    assert guide.get("type") != "string", guide
    assert "ReviewGuideDraft" in json.dumps(guide) + json.dumps(schema.get("$defs", {}))
    # The prose the table is written in survives into the schema the model reads.
    assert "replaces any earlier guide" in guide["description"]

    assert by_name["add_stage"].json_schema["properties"]["stages"]["type"] == "array"


def test_write_review_guide_stores_a_guide_sent_as_an_object(examples_root: Path) -> None:
    """Through the SDK tool, not the bound function: the bridge is what was broken."""
    _seed(examples_root, "congresswatch")
    _server, _allowed, tools = _build("congresswatch")
    by_name = {t.name: t for t in tools}

    saved = json.loads(_call(by_name["save_version"], {
        "project_id": "congresswatch", "message": "the loader alone",
        "parent_version": None})["content"][0]["text"])
    assert saved["ok"] is True, saved

    out = _call(by_name["write_review_guide"], {
        "project_id": "congresswatch",
        "version_id": saved["version_id"],
        "guide": {
            "steps": [{
                "title": "Load the filings",
                "prose": "Reads the filings as they were downloaded, one row each.",
                "stage_ids": ["load"],
                "data_description": "Every filing the download returned.",
            }],
            "unnarrated": [],
        },
    })
    assert not out.get("is_error"), out["content"][0]["text"]
    assert [step["title"] for step in json.loads(
        out["content"][0]["text"])["steps"]] == ["Load the filings"]


def test_a_parameter_the_function_does_not_take_is_refused() -> None:
    """The prose table is the only place a name can disagree with the function."""
    with pytest.raises(ValueError, match=r"does not take \['nonesuch'\]"):
        bind_by_signature(
            name="read_stage", description="d", fn=shared.read_stage, label="l",
            parameters={"nonesuch": "not a parameter of read_stage"},
        )


def test_a_parameter_no_prose_describes_is_refused() -> None:
    """The other half of the same seam: a signature that grew an argument silently."""
    def read_stage(project_id: str, stage_id: str, include_tests: bool = False) -> str:
        return ""

    with pytest.raises(ValueError, match=r"advertises \['include_tests'\]"):
        bind_by_signature(
            name="read_stage", description="d", fn=read_stage, label="l",
            parameters={
                "project_id": "The project's name.",
                "stage_id": "The stage's id, as read_workflow_summary shows it.",
            },
        )


def test_a_defaulted_parameter_is_optional_to_the_model() -> None:
    schema = next(iter(bind("run_stage_tests"))).json_schema
    assert set(schema["required"]) == {"project_id"}, schema["required"]
    assert "stage_id" in schema["properties"]


def test_a_caller_supplied_parameter_is_not_advertised() -> None:
    """base_url is the reader's address; the model cannot know it and is not asked."""
    schema = next(iter(bind("read_stage_output_rows"))).json_schema
    assert "base_url" not in schema["properties"]
