import json
from pathlib import Path
from typing import Any, Callable

import pytest

from app.agents.compiler.tools import EditingContext, make_editing_tools
from app.core.errors import RegenerateWithoutSnapshotError
from app.services import compilation, workspace

# Minimal valid handle block per stage type (app/models/stage.py:
# Stage._handle_for_type requires exactly one, keyed by `type`). Mirrors
# tests/test_workspace.py's _HANDLE_BY_TYPE so every fixture stage here
# round-trips through Stage.model_validate rather than landing in `issues`.
_HANDLE_BY_TYPE: dict[str, dict] = {
    "input_data": {"connector": {"kind": "file"}},
    "llm_transform": {"llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {row}"}},
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


# ─── compile_workflow (offline: monkeypatch the compiler, never call an LLM) ───

_FRESH_COMPILE_RESULT: dict[str, Any] = {
    "name": "alpha",
    "stages": [_stage("load", "Load rows", "input_data")],
    "methodology_raw": "# Methodology\ncompiled from doc",
    "compiler_notes": [],
    "validation": [],
    "prompt": "prompt text",
    "raw_llm": "raw llm text",
}

_INVALID_COMPILE_RESULT: dict[str, Any] = {
    "name": "alpha",
    "stages": [{"id": "load", "name": "Load rows", "type": "not_a_real_type"}],
    "methodology_raw": "# Methodology\nbad draft",
    "compiler_notes": [],
    "validation": ["load: unknown stage type 'not_a_real_type'"],
    "prompt": "prompt text",
    "raw_llm": "raw llm text",
}


# Compiler self-reports clean (validation: []), but a stage fails Stage/graph
# validation the compiler didn't run — an llm_transform with no 1:1 schemas. The
# write gate must catch it before anything is written.
_UNSOUND_COMPILE_RESULT: dict[str, Any] = {
    "name": "alpha",
    "stages": [
        _stage("load", "Load rows", "input_data"),
        _stage("score", "Score", "llm_transform", ["load"]),  # no schemas → not 1:1
    ],
    "methodology_raw": "# Methodology\ndraft",
    "compiler_notes": [],
    "validation": [],
    "prompt": "prompt text",
    "raw_llm": "raw llm text",
}


def _patch_compiler(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> None:
    monkeypatch.setattr(compilation, "compile_methodology", lambda text, name: result)


def test_compile_workflow_fresh_project_writes_compiled_dir(examples_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _seed(examples_root, "alpha")
    _patch_compiler(monkeypatch, _FRESH_COMPILE_RESULT)
    tools = _tools("alpha")

    out = _tool(tools, "compile_workflow")("alpha", "the conversation so far")

    assert out == {"ok": True, "stages": ["load"]}
    written = json.loads((pdir / "compiled" / "01_load.json").read_text(encoding="utf-8"))
    assert written["name"] == "Load rows"


def test_compile_workflow_rejects_unsound_draft_the_compiler_missed(
    examples_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdir = _seed(examples_root, "alpha")
    before = sorted(p.name for p in (pdir / "compiled").glob("*.json"))
    _patch_compiler(monkeypatch, _UNSOUND_COMPILE_RESULT)
    tools = _tools("alpha")

    out = _tool(tools, "compile_workflow")("alpha", "the conversation so far")

    assert out["ok"] is False and out["issues"]
    # nothing cleared or written — the existing compiled/ is untouched
    assert sorted(p.name for p in (pdir / "compiled").glob("*.json")) == before


def test_compile_workflow_reviewed_work_without_confirm_raises(examples_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _seed(examples_root, "alpha")
    _patch_compiler(monkeypatch, _FRESH_COMPILE_RESULT)
    tools = _tools("alpha")

    # Approve the seeded "load" stage so review work exists.
    from app.models import Stage
    from app.services import loader, node_review
    from app.services.versioning import list_versions

    seeded = json.loads((pdir / "compiled" / "01_load.json").read_text(encoding="utf-8"))
    current_hash = node_review.node_content_hash(loader.stage_to_spec_dict(Stage.model_validate(seeded)))
    node_review.record_node_decision(pdir, stage_id="load", content_hash=current_hash, decision="approve", reviewer="human")

    with pytest.raises(RegenerateWithoutSnapshotError):
        _tool(tools, "compile_workflow")("alpha", "the conversation so far")

    # nothing overwritten, no version created
    assert json.loads((pdir / "compiled" / "01_load.json").read_text(encoding="utf-8")) == seeded
    assert list_versions(pdir) == []


def test_compile_workflow_confirm_overwrite_snapshots_then_writes(examples_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _seed(examples_root, "alpha")
    _patch_compiler(monkeypatch, _FRESH_COMPILE_RESULT)
    tools = _tools("alpha")

    from app.models import Stage
    from app.services import loader, node_review
    from app.services.versioning import list_versions, load_version_stages

    seeded = json.loads((pdir / "compiled" / "01_load.json").read_text(encoding="utf-8"))
    current_hash = node_review.node_content_hash(loader.stage_to_spec_dict(Stage.model_validate(seeded)))
    node_review.record_node_decision(pdir, stage_id="load", content_hash=current_hash, decision="approve", reviewer="human")

    out = _tool(tools, "compile_workflow")("alpha", "the conversation so far", True)

    assert out == {"ok": True, "stages": ["load"]}
    versions = list_versions(pdir)
    assert len(versions) == 1
    assert versions[0].reviewer == "agent"
    # the snapshot preserves the PRE-regenerate (approved) spec
    [snapshotted_stage] = load_version_stages(pdir, versions[0].version_id)
    assert loader.stage_to_spec_dict(snapshotted_stage) == loader.stage_to_spec_dict(Stage.model_validate(seeded))


def test_compile_workflow_validation_issues_writes_nothing(examples_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _seed(examples_root, "alpha")
    before = (pdir / "compiled" / "01_load.json").read_text(encoding="utf-8")
    _patch_compiler(monkeypatch, _INVALID_COMPILE_RESULT)
    tools = _tools("alpha")

    out = _tool(tools, "compile_workflow")("alpha", "the conversation so far")

    assert out["ok"] is False
    assert out["issues"]
    assert (pdir / "compiled" / "01_load.json").read_text(encoding="utf-8") == before
    assert list((pdir / "compiled").glob("*.json")) == [pdir / "compiled" / "01_load.json"]


def test_compile_workflow_regenerate_to_fewer_stages_drops_stale_files(
    examples_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed a 2-stage project (load + score), both unreviewed so no confirm is
    # needed, then regenerate to a 1-stage result (load only). The dropped
    # "score" stage's compiled file must not survive the regenerate — leaving
    # it on disk would mean it keeps running while the tool reports it gone.
    pdir = _seed(examples_root, "alpha")
    (pdir / "compiled" / "02_score.json").write_text(
        json.dumps(_stage("score", "Score rows", "llm_transform", inputs=["load"])),
        encoding="utf-8",
    )
    _patch_compiler(monkeypatch, _FRESH_COMPILE_RESULT)  # load-only result
    tools = _tools("alpha")

    out = _tool(tools, "compile_workflow")("alpha", "the conversation so far")

    assert out == {"ok": True, "stages": ["load"]}

    from app.services import loader

    remaining = list((pdir / "compiled").glob("*.json"))
    assert loader.find_stage_file(pdir / "compiled", "score") is None
    assert len(remaining) == 1 and remaining[0].name.endswith("_load.json")

    summary = workspace.project_workflow_summary(pdir)
    assert [s["id"] for s in summary["stages"]] == ["load"]


def test_project_id_cannot_escape_the_workspace(examples_root: Path) -> None:
    _seed(examples_root, "alpha")
    tools = _tools("alpha")
    with pytest.raises(ValueError, match="invalid project id"):
        _tool(tools, "describe_workflow")("../outside")
