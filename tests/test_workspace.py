import json
from pathlib import Path

from app.services import workspace


# Stage validation (each type's model under app/models/stages/) requires one
# config block per type — an input_data stage needs `connector`, an
# llm_transform stage needs `llm` — or the tolerant loader reports it as an
# issue rather than a parsed stage (see tests/test_loader.py's _valid fixture
# for the same pattern). Minimal valid config blocks per type, added only when the
# type needs one, so a stage written by this helper always round-trips through
# parse_stage.
def _config_block_by_type(root: Path) -> dict[str, dict]:
    return {
        "input_data": {"connector": {"kind": "file",
                                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}}},
        "llm_transform": {"llm": {"prompt_template": "score {doc_id}"}},
    }


_LLM_IN_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": False}]}
_LLM_OUT_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": False},
                               {"name": "score", "type": "float", "nullable": False}]}


def _write_stage(compiled: Path, order: int, sid: str, stype: str, inputs: list[str]) -> None:
    compiled.mkdir(parents=True, exist_ok=True)
    stage: dict = {"id": sid, "description": f"{sid} step", "type": stype}
    stage.update(_config_block_by_type(compiled.parent).get(stype, {}))
    # Every input declares the schema it expects and every stage declares a
    # signature (app/models/stages/stage_base.py: StageBase._schemas_declared).
    # llm_transform is additionally strictly 1:1, so it must add a column.
    stage["signature"] = (
        # An llm_transform's reads must match its template's placeholders exactly.
        {"form": "extends",
         "reads": [{"input": inputs[0], "columns": _LLM_IN_SCHEMA["columns"]}],
         "adds": [c for c in _LLM_OUT_SCHEMA["columns"]
                  if c not in _LLM_IN_SCHEMA["columns"]]}
        if stype == "llm_transform"
        else {"form": "replaces", "produces": _LLM_IN_SCHEMA["columns"]})
    if inputs:
        stage["inputs"] = [{"id": dep, "schema": _LLM_IN_SCHEMA} for dep in inputs]
    (compiled / f"{order:02d}_{sid}.json").write_text(json.dumps(stage), encoding="utf-8")


def test_list_project_names_only_dirs_with_compiled(tmp_path: Path) -> None:
    workspace.set_projects_dir(tmp_path)
    _write_stage(tmp_path / "alpha" / "compiled", 1, "load", "input_data", [])
    (tmp_path / "not_a_project").mkdir()
    assert workspace.list_project_names() == ["alpha"]


def test_workflow_summary_reports_ids_types_inputs_and_review_state(tmp_path: Path) -> None:
    pdir = tmp_path / "alpha"
    _write_stage(pdir / "compiled", 1, "load", "input_data", [])
    _write_stage(pdir / "compiled", 2, "score", "llm_transform", ["load"])
    summary = workspace.project_workflow_summary(pdir)
    assert summary["name"] == "alpha"
    by_id = {s["id"]: s for s in summary["stages"]}
    assert by_id["score"]["type"] == "llm_transform"
    assert by_id["score"]["inputs"] == ["load"]
    assert by_id["load"]["review_state"] == "unreviewed"
    assert summary["issues"] == []
