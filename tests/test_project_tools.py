import json
from pathlib import Path
from typing import Callable

import pytest

from app.agents.compiler.tools import EditingContext, make_editing_tools
from app.core.errors import ReviewGuideValidationError
from app.services import workspace
from app.services.project import Project

# Minimal valid config block per stage type (app/models/stage.py:
# each type's stage model declares the ones it requires). Mirrors
# tests/test_workspace.py's _HANDLE_BY_TYPE so every fixture stage here
# round-trips through parse_stage rather than landing in `issues`.
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
    which resolve names against the projects root internally — read and
    write there rather than the real workspace."""
    workspace.set_projects_dir(tmp_path)
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
    """A project on disk AND in the store. Identity is the Project record, so a
    staged directory alone is not a project — list_projects() reads the store."""
    compiled = examples / name / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "01_load.json").write_text(
        json.dumps(_stage("load", "Load rows", "input_data")), encoding="utf-8"
    )
    Project(id=name).save()
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


# ── the review-guide tools ───────────────────────────────────────────────────

def _versioned(examples: Path, name: str) -> tuple[list[Callable], str]:
    """A saved two-stage version, reached the way the agent reaches one: draft the
    stages, then save. Two stages, so a guide can narrate one and leave the other out."""
    _seed(examples, name)
    tools = _tools(name)
    draft = _tool(tools, "create_draft")(name)
    for stage in (
        _stage("load", "Load rows", "input_data"),
        _stage("score", "Score rows", "llm_transform", inputs=["load"]),
    ):
        _tool(tools, "set_draft_stage")(name, draft.id, json.dumps(stage))
    saved = _tool(tools, "save_version")(name, draft.id, "first proposal")
    assert saved.version_id is not None
    return tools, saved.version_id


def _guide(step_ids: list[str], unnarrated: list[str]) -> str:
    return json.dumps(
        {
            "steps": [
                {
                    "title": "Score each row",
                    "prose": "Every row keeps its `id` and is scored as reported.",
                    "stage_ids": step_ids,
                }
            ],
            "unnarrated": unnarrated,
        }
    )


def test_read_review_guide_is_null_until_one_is_written(examples_root: Path) -> None:
    """A version is born without a guide, and nothing seeds one — the tool says so
    rather than returning an empty guide that would read as an authored decision."""
    tools, version_id = _versioned(examples_root, "alpha")
    assert _tool(tools, "read_review_guide")("alpha", version_id) is None


def test_write_review_guide_round_trips_through_read(examples_root: Path) -> None:
    tools, version_id = _versioned(examples_root, "alpha")
    written = _tool(tools, "write_review_guide")("alpha", version_id, _guide(["load"], ["score"]))

    stored = _tool(tools, "read_review_guide")("alpha", version_id)
    assert stored == written
    assert [step.title for step in stored.steps] == ["Score each row"]
    assert stored.collect_step_stage_ids() == ["load"]
    assert stored.unnarrated == ["score"]


@pytest.mark.parametrize(
    "step_ids, unnarrated, named",
    [
        (["load", "ghost"], ["score"], "ghost"),  # no stage in the version has this id
        (["load"], [], "score"),  # accounted for by neither a step nor unnarrated
        (["load", "score"], ["load"], "load"),  # narrated AND declared unnarrated
    ],
)
def test_write_review_guide_rejects_a_mismatch_naming_the_stage(
    examples_root: Path, step_ids: list[str], unnarrated: list[str], named: str
) -> None:
    """Each way a guide can misaccount for its version's stages is refused with the
    offending id in the message — the agent can fix it without reading the version."""
    tools, version_id = _versioned(examples_root, "alpha")
    with pytest.raises(ReviewGuideValidationError, match=named):
        _tool(tools, "write_review_guide")("alpha", version_id, _guide(step_ids, unnarrated))
    assert _tool(tools, "read_review_guide")("alpha", version_id) is None


def test_write_review_guide_rejects_a_stage_narrated_by_two_steps(examples_root: Path) -> None:
    tools, version_id = _versioned(examples_root, "alpha")
    two_steps = json.dumps(
        {
            "steps": [
                {"title": "Load", "prose": "Reads the rows.", "stage_ids": ["load"]},
                {"title": "Load again", "prose": "Reads them again.", "stage_ids": ["load"]},
            ],
            "unnarrated": ["score"],
        }
    )
    with pytest.raises(ReviewGuideValidationError, match="load"):
        _tool(tools, "write_review_guide")("alpha", version_id, two_steps)
    assert _tool(tools, "read_review_guide")("alpha", version_id) is None


def test_write_review_guide_rejects_an_invented_field(examples_root: Path) -> None:
    """The guide model forbids extras, so a field the agent made up is refused rather
    than dropped — a guide that silently loses what was written is worse than none."""
    tools, version_id = _versioned(examples_root, "alpha")
    invented = json.dumps(
        {
            "steps": [{"title": "Load", "prose": "Reads the rows.", "stage_ids": ["load"],
                       "confidence": "high"}],
            "unnarrated": ["score"],
        }
    )
    with pytest.raises(ValueError, match="confidence"):
        _tool(tools, "write_review_guide")("alpha", version_id, invented)
    assert _tool(tools, "read_review_guide")("alpha", version_id) is None


def test_a_rejected_write_leaves_the_stored_guide_untouched(examples_root: Path) -> None:
    """The refusal is not a delete: the version keeps the guide it already had."""
    tools, version_id = _versioned(examples_root, "alpha")
    kept = _tool(tools, "write_review_guide")("alpha", version_id, _guide(["load"], ["score"]))

    with pytest.raises(ReviewGuideValidationError):
        _tool(tools, "write_review_guide")("alpha", version_id, _guide(["load", "ghost"], ["score"]))

    assert _tool(tools, "read_review_guide")("alpha", version_id) == kept


def test_write_review_guide_replaces_the_whole_guide(examples_root: Path) -> None:
    tools, version_id = _versioned(examples_root, "alpha")
    _tool(tools, "write_review_guide")("alpha", version_id, _guide(["load"], ["score"]))
    _tool(tools, "write_review_guide")("alpha", version_id, _guide(["load", "score"], []))

    stored = _tool(tools, "read_review_guide")("alpha", version_id)
    assert stored.collect_step_stage_ids() == ["load", "score"]
    assert stored.unnarrated == []


def test_read_review_guide_of_an_unknown_version_fails_loud(examples_root: Path) -> None:
    tools, _version_id = _versioned(examples_root, "alpha")
    with pytest.raises(FileNotFoundError):
        _tool(tools, "read_review_guide")("alpha", "no_such_version")
