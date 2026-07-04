import json
from pathlib import Path
from typing import Any, Callable

import pytest

from app.chat import project_tools
from app.errors import RegenerateWithoutSnapshotError

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


def test_edit_stage_tool_writes_and_reports_state(tmp_path: Path) -> None:
    pdir = _seed(tmp_path, "alpha")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)
    out = _tool(tools, "edit_stage")(
        "load", json.dumps(_stage("load", "Load rows v2", "input_data"))
    )
    assert out["ok"] is True and out["state"] == "unreviewed"
    assert "Load rows v2" in (pdir / "compiled" / "01_load.json").read_text(encoding="utf-8")


def test_edit_stage_tool_invalid_writes_nothing_and_reports_issues(tmp_path: Path) -> None:
    pdir = _seed(tmp_path, "alpha")
    before = (pdir / "compiled" / "01_load.json").read_text(encoding="utf-8")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)
    out = _tool(tools, "edit_stage")(
        "load", json.dumps({"id": "load", "name": "x", "type": "not_a_real_type"})
    )
    assert out["ok"] is False and out["issues"]
    assert (pdir / "compiled" / "01_load.json").read_text(encoding="utf-8") == before


def test_create_version_tool_snapshots_as_agent(tmp_path: Path) -> None:
    _seed(tmp_path, "alpha")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)
    out = _tool(tools, "create_version")("first snapshot")
    assert out["reviewer"] == "agent"
    assert (tmp_path / "alpha" / "versions" / out["id"] / "version.json").exists()


_DOC_TEXT = "\n".join(
    [
        "# Methodology",
        "intro line",
        "## Sourcing",
        "sourcing line one",
        "sourcing line two mentions ICIJ",
        "## Scoring",
        "scoring line one",
        "# Appendix",
        "appendix line",
    ]
)


def test_fetch_document_copies_and_returns_outline_not_body(tmp_path: Path) -> None:
    _seed(tmp_path, "alpha")
    source_root = tmp_path / "outside_project"
    source_root.mkdir()
    src = source_root / "methodology.md"
    src.write_text(_DOC_TEXT, encoding="utf-8")

    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)
    out = _tool(tools, "fetch_document")(str(src))

    assert out["path"] == str(tmp_path / "alpha" / "source" / "methodology.md")
    assert out["bytes"] == src.stat().st_size
    assert out["lines"] == len(_DOC_TEXT.splitlines())
    assert out["headings"] == ["# Methodology", "## Sourcing", "## Scoring", "# Appendix"]
    # never the body
    assert "sourcing line one" not in json.dumps(out)
    assert Path(out["path"]).read_text(encoding="utf-8") == _DOC_TEXT


def test_fetch_document_missing_path_fails_loud(tmp_path: Path) -> None:
    _seed(tmp_path, "alpha")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)
    with pytest.raises(ValueError, match="no document at"):
        _tool(tools, "fetch_document")(str(tmp_path / "nope.md"))


def test_read_section_returns_bounded_slice_between_headings(tmp_path: Path) -> None:
    _seed(tmp_path, "alpha")
    doc = tmp_path / "doc.md"
    doc.write_text(_DOC_TEXT, encoding="utf-8")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)

    section = _tool(tools, "read_section")(str(doc), "Sourcing")

    assert section.splitlines()[0] == "## Sourcing"
    assert "sourcing line one" in section
    assert "sourcing line two mentions ICIJ" in section
    # stops before the next same-or-higher-level heading
    assert "## Scoring" not in section
    assert "scoring line one" not in section


def test_read_section_missing_heading_fails_loud(tmp_path: Path) -> None:
    _seed(tmp_path, "alpha")
    doc = tmp_path / "doc.md"
    doc.write_text(_DOC_TEXT, encoding="utf-8")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)
    with pytest.raises(ValueError, match="no heading matching"):
        _tool(tools, "read_section")(str(doc), "NoSuchHeading")


def test_read_section_caps_at_400_lines(tmp_path: Path) -> None:
    _seed(tmp_path, "alpha")
    long_body = "\n".join(f"line {i}" for i in range(500))
    doc = tmp_path / "long.md"
    doc.write_text(f"# Big\n{long_body}\n# Next\nafter", encoding="utf-8")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)

    section = _tool(tools, "read_section")(str(doc), "Big")

    assert len(section.splitlines()) == 400


def test_grep_doc_returns_matching_lines_with_line_numbers(tmp_path: Path) -> None:
    _seed(tmp_path, "alpha")
    doc = tmp_path / "doc.md"
    doc.write_text(_DOC_TEXT, encoding="utf-8")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)

    matches = _tool(tools, "grep_doc")(str(doc), "icij")  # case-insensitive

    lines = matches.splitlines()
    assert len(lines) == 1
    assert lines[0] == "5: sourcing line two mentions ICIJ"


def test_grep_doc_caps_at_50_lines(tmp_path: Path) -> None:
    _seed(tmp_path, "alpha")
    doc = tmp_path / "many.md"
    doc.write_text("\n".join("needle here" for _ in range(80)), encoding="utf-8")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)

    matches = _tool(tools, "grep_doc")(str(doc), "needle")

    assert len(matches.splitlines()) == 50


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


def _patch_compiler(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any], doc_text: str = "prose") -> None:
    monkeypatch.setattr(project_tools, "read_input", lambda path: doc_text)
    monkeypatch.setattr(project_tools, "compile_prose_to_workflow", lambda text, name: result)


def test_compile_workflow_fresh_project_writes_compiled_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _seed(tmp_path, "alpha")
    _patch_compiler(monkeypatch, _FRESH_COMPILE_RESULT)
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)

    doc = tmp_path / "doc.md"
    doc.write_text("prose", encoding="utf-8")
    out = _tool(tools, "compile_workflow")(str(doc))

    assert out == {"ok": True, "stages": ["load"]}
    written = json.loads((pdir / "compiled" / "01_load.json").read_text(encoding="utf-8"))
    assert written["name"] == "Load rows"


def test_compile_workflow_reviewed_work_without_confirm_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _seed(tmp_path, "alpha")
    _patch_compiler(monkeypatch, _FRESH_COMPILE_RESULT)
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)

    # Approve the seeded "load" stage so review work exists.
    from app.models import Stage
    from app.services import loader, node_review

    seeded = json.loads((pdir / "compiled" / "01_load.json").read_text(encoding="utf-8"))
    current_hash = node_review.node_content_hash(loader.stage_to_spec_dict(Stage.model_validate(seeded)))
    node_review.record_node_decision(pdir, stage_id="load", content_hash=current_hash, decision="approve", reviewer="human")

    doc = tmp_path / "doc.md"
    doc.write_text("prose", encoding="utf-8")
    with pytest.raises(RegenerateWithoutSnapshotError):
        _tool(tools, "compile_workflow")(str(doc))

    # nothing overwritten, no version created
    assert json.loads((pdir / "compiled" / "01_load.json").read_text(encoding="utf-8")) == seeded
    assert not (pdir / "versions").exists()


def test_compile_workflow_confirm_overwrite_snapshots_then_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _seed(tmp_path, "alpha")
    _patch_compiler(monkeypatch, _FRESH_COMPILE_RESULT)
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)

    from app.models import Stage
    from app.services import loader, node_review

    seeded = json.loads((pdir / "compiled" / "01_load.json").read_text(encoding="utf-8"))
    current_hash = node_review.node_content_hash(loader.stage_to_spec_dict(Stage.model_validate(seeded)))
    node_review.record_node_decision(pdir, stage_id="load", content_hash=current_hash, decision="approve", reviewer="human")

    doc = tmp_path / "doc.md"
    doc.write_text("prose", encoding="utf-8")
    out = _tool(tools, "compile_workflow")(str(doc), True)

    assert out == {"ok": True, "stages": ["load"]}
    versions = list((pdir / "versions").iterdir())
    assert len(versions) == 1
    snapshot_meta = json.loads((versions[0] / "version.json").read_text(encoding="utf-8"))
    assert snapshot_meta["reviewer"] == "agent"
    # the snapshot preserves the PRE-regenerate (approved) spec
    snapshotted_stage = json.loads((versions[0] / "compiled" / "01_load.json").read_text(encoding="utf-8"))
    assert snapshotted_stage == seeded


def test_compile_workflow_validation_issues_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _seed(tmp_path, "alpha")
    before = (pdir / "compiled" / "01_load.json").read_text(encoding="utf-8")
    _patch_compiler(monkeypatch, _INVALID_COMPILE_RESULT)
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)

    doc = tmp_path / "doc.md"
    doc.write_text("prose", encoding="utf-8")
    out = _tool(tools, "compile_workflow")(str(doc))

    assert out["ok"] is False
    assert out["issues"] == _INVALID_COMPILE_RESULT["validation"]
    assert (pdir / "compiled" / "01_load.json").read_text(encoding="utf-8") == before
    assert list((pdir / "compiled").glob("*.json")) == [pdir / "compiled" / "01_load.json"]


def test_compile_workflow_regenerate_to_fewer_stages_drops_stale_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed a 2-stage project (load + score), both unreviewed so no confirm is
    # needed, then regenerate to a 1-stage result (load only). The dropped
    # "score" stage's compiled file must not survive the regenerate — leaving
    # it on disk would mean it keeps running while the tool reports it gone.
    pdir = _seed(tmp_path, "alpha")
    (pdir / "compiled" / "02_score.json").write_text(
        json.dumps(_stage("score", "Score rows", "llm_transform", inputs=["load"])),
        encoding="utf-8",
    )
    _patch_compiler(monkeypatch, _FRESH_COMPILE_RESULT)  # load-only result
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)

    doc = tmp_path / "doc.md"
    doc.write_text("prose", encoding="utf-8")
    out = _tool(tools, "compile_workflow")(str(doc))

    assert out == {"ok": True, "stages": ["load"]}

    from app.services import loader

    remaining = list((pdir / "compiled").glob("*.json"))
    assert loader.find_stage_file(pdir / "compiled", "score") is None
    assert len(remaining) == 1 and remaining[0].name.endswith("_load.json")

    from app.services import workspace

    summary = workspace.project_workflow_summary(pdir)
    assert [s["id"] for s in summary["stages"]] == ["load"]
