import json
from pathlib import Path
from typing import Callable

import pytest

from app.models.stage import StageEdit
from pydantic import ValidationError

from app.tools.editing import EditingContext, build_editing_tools
from app.core.agent.bound_tool import BoundToolSpec
from app.core.errors import ReviewGuideValidationError
from app.models.review_guide import ReviewGuideDraft, ReviewGuideStep
from app.models.records.review_guide import ReviewGuide
from app.services import workspace
from app.models.records.project import Project
from stage_seed import add_stage, read_stage

# Minimal valid config block per stage type (app/models/stage.py:
# each type's stage model declares the ones it requires). Mirrors
# tests/test_workspace.py's _HANDLE_BY_TYPE so every fixture stage here
# round-trips through parse_stage rather than landing in `issues`.
_HANDLE_BY_TYPE: dict[str, dict] = {
    "input_data": {"connector": {"kind": "file"}},
    "llm_transform": {"llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {row}"}},
}

# Stage requires a schema on every input and a signature saying what it outputs.
# The only producer these fixtures build is `load`, so an input edge always
# carries `load`'s produced schema; the llm_transform adds `score` on top of it,
# so it stays strictly 1:1.
_LOAD_COLUMNS: list[dict] = [
    {"name": "id", "type": "str", "nullable": False},
    {"name": "row", "type": "str", "nullable": False},
]
_LOAD_SCHEMA: dict = {"columns": _LOAD_COLUMNS}
_SCORE_SCHEMA: dict = {
    "columns": [*_LOAD_COLUMNS, {"name": "score", "type": "float", "nullable": True}],
}
_SIGNATURE_BY_TYPE: dict[str, dict] = {
    "input_data": {"form": "replaces", "produces": _LOAD_COLUMNS},
    "llm_transform": {
        "form": "extends",
        "reads": [{"input": "load", "columns": [
            {"name": "row", "type": "str", "nullable": False}]}],
        "adds": [{"name": "score", "type": "float", "nullable": True}],
    },
}


@pytest.fixture(autouse=True)
def examples_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _tools(name: str) -> list[BoundToolSpec]:
    return build_editing_tools(EditingContext(project_id=name, base_url="http://reader.test/"))


def _stage(sid: str, name: str, stype: str, inputs: list[str] | None = None) -> dict:
    stage: dict = {"id": sid, "description": name, "type": stype}
    stage.update(_HANDLE_BY_TYPE.get(stype, {}))
    if stype in _SIGNATURE_BY_TYPE:
        stage["signature"] = _SIGNATURE_BY_TYPE[stype]
    if inputs:
        stage["inputs"] = [{"id": dep} for dep in inputs]
    return stage


def _seed(examples: Path, name: str) -> Path:
    pdir = examples / name
    pdir.mkdir(parents=True, exist_ok=True)
    add_stage(pdir, _stage("load", "Load rows", "input_data"))
    # The directory's name IS the id — this seeds one directly rather than through
    # create_project, so the record must carry the same id the directory does.
    Project(id=name, name=name).save()
    return examples / name


def _tool(specs: list[BoundToolSpec], fn_name: str) -> Callable:
    for spec in specs:
        if spec.name == fn_name:
            return spec.fn
    raise AssertionError(f"tool {fn_name!r} not registered")


def test_read_tools_report_workspace(examples_root: Path) -> None:
    _seed(examples_root, "alpha")
    tools = _tools("alpha")
    assert [(p.id, p.name) for p in _tool(tools, "list_projects")()] == [("alpha", "alpha")]

    assert _tool(tools, "read_workflow_summary")("alpha").name == "alpha"
    assert '"id": "load"' in _tool(tools, "read_stage")("alpha", "load")


def test_read_stage_missing_fails_loud(examples_root: Path) -> None:
    _seed(examples_root, "alpha")
    tools = _tools("alpha")
    with pytest.raises(ValueError, match="no stage 'nope'"):
        _tool(tools, "read_stage")("alpha", "nope")


def test_edit_stage_tool_writes_and_reports_ok(examples_root: Path) -> None:
    pdir = _seed(examples_root, "alpha")
    tools = _tools("alpha")
    out = _tool(tools, "edit_stages")(
        "alpha", [StageEdit(stage_id="load",
                            changes_json=json.dumps(_stage("load", "Load rows v2", "input_data")))]
    )
    # The review colour is the review layer's, never the writer's.
    assert out.ok is True and out.edited == ["load"] and out.issues == []
    assert read_stage(pdir, "load")["description"] == "Load rows v2"


def test_edit_stage_tool_invalid_writes_nothing_and_reports_issues(examples_root: Path) -> None:
    pdir = _seed(examples_root, "alpha")
    before = read_stage(pdir, "load")
    tools = _tools("alpha")
    out = _tool(tools, "edit_stages")(
        "alpha", [StageEdit(stage_id="load",
                            changes_json=json.dumps({"id": "load", "description": "x",
                                                     "type": "not_a_real_type"}))]
    )
    assert out.ok is False and out.issues and out.edited == []
    assert read_stage(pdir, "load") == before


def test_project_id_cannot_escape_the_workspace(examples_root: Path) -> None:
    _seed(examples_root, "alpha")
    tools = _tools("alpha")
    with pytest.raises(ValueError, match="invalid project id"):
        _tool(tools, "read_workflow_summary")("../outside")


# ── the review-guide tools ───────────────────────────────────────────────────

def _versioned(examples: Path, name: str) -> tuple[list[BoundToolSpec], str]:
    _seed(examples, name)
    tools = _tools(name)
    # _seed already wrote `load`; the guide these tests write needs a stage above it.
    add_stage(examples / name, _stage("score", "Score rows", "llm_transform", inputs=["load"]))
    saved = _tool(tools, "save_version")(name, "first proposal")
    assert saved["version_id"] is not None
    return tools, saved["version_id"]


def _guide(step_ids: list[str], unnarrated: list[str]) -> ReviewGuideDraft:
    return ReviewGuideDraft(
        steps=[
            ReviewGuideStep(
                title="Score each row",
                prose="Every row keeps its `id` and is scored as reported.",
                stage_ids=step_ids,
                data_description="Every row as reported, carrying its score.",
            )
        ],
        unnarrated=unnarrated,
    )


def test_read_review_guide_is_null_until_one_is_written(examples_root: Path) -> None:
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
    tools, version_id = _versioned(examples_root, "alpha")
    with pytest.raises(ReviewGuideValidationError, match=named):
        _tool(tools, "write_review_guide")("alpha", version_id, _guide(step_ids, unnarrated))
    assert _tool(tools, "read_review_guide")("alpha", version_id) is None


def test_write_review_guide_rejects_a_stage_narrated_by_two_steps(examples_root: Path) -> None:
    tools, version_id = _versioned(examples_root, "alpha")
    two_steps = ReviewGuideDraft(
        steps=[
            ReviewGuideStep(title="Load", prose="Reads the rows.", stage_ids=["load"],
                            data_description="The rows as filed."),
            ReviewGuideStep(title="Load again", prose="Reads them again.",
                            stage_ids=["load"],
                            data_description="The same rows, read a second time."),
        ],
        unnarrated=["score"],
    )
    with pytest.raises(ReviewGuideValidationError, match="load"):
        _tool(tools, "write_review_guide")("alpha", version_id, two_steps)
    assert _tool(tools, "read_review_guide")("alpha", version_id) is None


def test_write_review_guide_rejects_an_invented_field() -> None:
    """The tool binds its JSON to ReviewGuide, so the model refuses before any tool code runs."""
    with pytest.raises(ValidationError, match="confidence"):
        ReviewGuide.model_validate(
            {
                "steps": [{"title": "Load", "prose": "Reads the rows.",
                           "stage_ids": ["load"], "confidence": "high"}],
                "unnarrated": ["score"],
            }
        )


def test_a_rejected_write_leaves_the_stored_guide_untouched(examples_root: Path) -> None:
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
