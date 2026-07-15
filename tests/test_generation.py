"""Generation's WORKFLOW build: start_workflow_generation delegates to the app.compiler.workflow
bridge (start_workflow_generation_agent) with an on_answer that persists the submitted Workflow via
_finish_workflow, and never touches schemas/ (workflow-only). The bridge's own live-turn
machinery is tested in test_compile_workflow.py; here we test the generation-side wiring with
the bridge stubbed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import app.services.generation as generation
from app.core.models import parse_schema_library
from app.core.models.workflow import Workflow

_DM = parse_schema_library([{
    "name": "documents", "title": "Documents", "kind": "input",
    "description": "source docs", "primary_key": ["doc_id"],
    "columns": [{"name": "doc_id", "type": "str", "description": "id"}],
}])

_STAGE = {
    "id": "load", "type": "input_data", "name": "Load documents",
    "connector": {"kind": "file", "path": "data/docs.csv", "format": "csv"},
    "output_schema": {"columns": [{"name": "doc_id", "type": "str"}]},
}


# ── _finish_workflow: write on a valid submission only ────────────────────────────────

def test_finish_workflow_writes_the_workflow_on_success(tmp_path: Path, monkeypatch: Any):
    wrote: dict = {}
    monkeypatch.setattr(
        generation, "regenerate_workflow",
        lambda result, project_dir: wrote.update(result=result),
    )

    generation._finish_workflow(tmp_path, "demo", Workflow.model_validate({"stages": [_STAGE]}))

    assert [s["id"] for s in wrote["result"]["stages"]] == ["load"]   # Workflow -> canonical dicts
    assert wrote["result"]["validation"] == []


def test_finish_workflow_writes_nothing_without_a_submission(tmp_path: Path, monkeypatch: Any):
    wrote: dict = {}
    monkeypatch.setattr(
        generation, "regenerate_workflow",
        lambda result, project_dir: wrote.setdefault("x", True),
    )

    generation._finish_workflow(tmp_path, "demo", None)

    assert "x" not in wrote   # nothing built on a workflow the agent never produced


# ── start_workflow_generation: delegates to the bridge, grounded, on_answer persists ──

def test_start_workflow_generation_delegates_to_the_bridge_grounded(
    tmp_path: Path, monkeypatch: Any
):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    captured: dict = {}

    def fake_bridge(*, document, project_name, model, data_model, on_answer):
        captured.update(
            document=document, project_name=project_name, model=model,
            data_model=data_model, on_answer=on_answer,
        )
        return "sess-xyz"

    monkeypatch.setattr(generation, "start_workflow_generation_agent", fake_bridge)

    sid = generation.start_workflow_generation(
        project_dir, document="doc", model="sonnet", data_model=_DM
    )

    assert sid == "sess-xyz"                # returns the bridge's session id (route → /chat/<sid>)
    assert captured["project_name"] == "demo"
    assert captured["model"] == "sonnet"
    assert captured["data_model"] is _DM    # the approved data model grounds the compile

    # the on_answer handed to the bridge persists a submitted Workflow via _finish_workflow
    written: dict = {}
    monkeypatch.setattr(
        generation, "regenerate_workflow",
        lambda result, project_dir: written.update(result=result),
    )
    captured["on_answer"](Workflow.model_validate({"stages": [_STAGE]}))

    assert [s["id"] for s in written["result"]["stages"]] == ["load"]  # workflow written on submit
    assert not (project_dir / "schemas").exists()                       # workflow-only: schemas untouched
