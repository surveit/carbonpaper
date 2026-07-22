import json
from pathlib import Path

from app.services import workspace


# Stage validation (app/models/stage.py: Stage._handle_for_type) requires one
# handle block per type — an input_data stage needs `connector`, an
# llm_transform stage needs `llm` — or the tolerant loader reports it as an
# issue rather than a parsed stage (see tests/test_loader.py's _valid fixture
# for the same pattern). Minimal valid handles per type, added only when the
# type needs one, so a stage written by this helper always round-trips through
# Stage.model_validate.
def _handle_by_type(root: Path) -> dict[str, dict]:
    return {
        "input_data": {"connector": {"kind": "file",
                                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}}},
        "llm_transform": {"llm": {"prompt_template": "score {row}"}},
    }


_LLM_IN_SCHEMA = {"primary_key": ["doc_id"],
                  "columns": [{"name": "doc_id", "type": "str", "nullable": False}]}
_LLM_OUT_SCHEMA = {"primary_key": ["doc_id"],
                   "columns": [{"name": "doc_id", "type": "str", "nullable": False},
                               {"name": "score", "type": "float", "nullable": False}]}


def _write_stage(compiled: Path, order: int, sid: str, stype: str, inputs: list[str]) -> None:
    compiled.mkdir(parents=True, exist_ok=True)
    stage: dict = {"id": sid, "name": f"{sid} step", "type": stype}
    stage.update(_handle_by_type(compiled.parent).get(stype, {}))
    if inputs:
        # llm_transform is strictly 1:1 (app/models/stage.py): its input and output
        # schemas must share a primary_key and the output must add a column.
        if stype == "llm_transform":
            stage["inputs"] = [{"id": dep, "schema": _LLM_IN_SCHEMA} for dep in inputs]
            stage["output_schema"] = _LLM_OUT_SCHEMA
        else:
            stage["inputs"] = [{"id": dep} for dep in inputs]
    (compiled / f"{order:02d}_{sid}.json").write_text(json.dumps(stage), encoding="utf-8")


def test_list_project_names_only_dirs_with_compiled(tmp_path: Path) -> None:
    _write_stage(tmp_path / "alpha" / "compiled", 1, "load", "input_data", [])
    (tmp_path / "not_a_project").mkdir()
    assert workspace.list_project_names(tmp_path) == ["alpha"]


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
