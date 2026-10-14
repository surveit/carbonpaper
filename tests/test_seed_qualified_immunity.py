from __future__ import annotations

import json
from pathlib import Path

from app.packs.opinions.connector import CourtListenerOpinionsParams
from app.services import workflow_summary
from app.services.project import WorkflowFile, import_project

_BUNDLE = (Path(__file__).resolve().parents[1] / "app" / "seeds" / "data"
           / "qualified_immunity_appeals.json")


def test_the_bundle_imports_with_no_issues() -> None:
    project_id = import_project(WorkflowFile.model_validate_json(_BUNDLE.read_text(encoding="utf-8")))
    assert workflow_summary.read_workflow_summary(project_id).issues == []


def test_the_input_stage_names_a_valid_opinion_search() -> None:
    bundle = json.loads(_BUNDLE.read_text(encoding="utf-8"))
    (stage,) = [s for s in bundle["stages"] if s["type"] == "input_data"]
    assert stage["connector"]["kind"] == "courtlistener_opinions"
    CourtListenerOpinionsParams.model_validate(stage["connector"]["params"])
