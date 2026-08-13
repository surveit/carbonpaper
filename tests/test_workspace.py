from pathlib import Path

from app.core.paths import CARBON_PAPER_HOME
from app.services import workspace
from stage_seed import add_stage


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


def _write_stage(pdir: Path, order: int, sid: str, stype: str, inputs: list[str]) -> None:
    pdir.mkdir(parents=True, exist_ok=True)
    stage: dict = {"id": sid, "description": f"{sid} step", "type": stype}
    stage.update(_config_block_by_type(pdir).get(stype, {}))
    # Every input declares the schema it expects and every stage declares a
    # signature (app/models/stages/stage_base.py: AbstractStage._schemas_declared).
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
        stage["inputs"] = [{"id": dep} for dep in inputs]
    add_stage(pdir, stage)


def test_workflow_summary_reports_ids_types_and_inputs(tmp_path: Path) -> None:
    pdir = tmp_path / "alpha"
    _write_stage(pdir, 1, "load", "input_data", [])
    _write_stage(pdir, 2, "score", "llm_transform", ["load"])
    summary = workspace.project_workflow_summary(pdir.name)
    assert summary.name == "alpha"
    by_id = {s.id: s for s in summary.stages}
    assert by_id["score"].type == "llm_transform"
    assert by_id["score"].inputs == ["load"]
    assert summary.issues == []


def test_the_projects_root_defaults_to_the_machine_global_home(monkeypatch) -> None:
    monkeypatch.setattr(workspace, "_projects_dir", None)
    assert workspace.projects_dir() == CARBON_PAPER_HOME / "examples"
