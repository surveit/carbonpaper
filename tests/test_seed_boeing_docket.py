"""The Boeing docket chronology bundle imports, publishes the shared tables, and reads offline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.evals.compatibility import validate_eval_compatibility
from app.evals.dataset import read_table_ref
from app.models import TableSchema, Workflow
from app.models.chronology import DISPUTE_COLUMNS, EVENT_COLUMNS
from app.models.connectors import MirroredBytes
from app.models.records.eval_config import EvalConfig
from app.packs.docket import connector
from app.packs.docket.connector import RecapDocketParams, acquire_recap_docket
from app.services import versioning, workflow_summary
from app.services.project import WorkflowFile, import_project

_SEEDS = Path(__file__).resolve().parents[1] / "app" / "seeds" / "data"
_BUNDLE = _SEEDS / "boeing_docket_chronology.json"
_EVAL = _SEEDS / "evals" / "boeing_docket_chronology.json"


def _import_bundle() -> str:
    return import_project(WorkflowFile.model_validate_json(_BUNDLE.read_text(encoding="utf-8")))


def _read_input_params() -> dict[str, object]:
    bundle = json.loads(_BUNDLE.read_text(encoding="utf-8"))
    (stage,) = [stage for stage in bundle["stages"] if stage["id"] == "input_filings"]
    return stage["connector"]["params"]


def test_the_bundle_imports_with_no_issues() -> None:
    project_id = _import_bundle()
    assert workflow_summary.read_workflow_summary(project_id).issues == []


def _load_imported_workflow(project_id: str) -> Workflow:
    version_id = versioning.find_latest_version_id(project_id)
    assert version_id is not None
    return Workflow(stages=versioning.load_version_stages(project_id, version_id))


def test_the_published_tables_hold_the_shared_chronology_columns() -> None:
    by_id = _load_imported_workflow(_import_bundle()).index_workflow_stages_by_id()
    for stage_id, wanted in (("chronology", EVENT_COLUMNS), ("disputes", DISPUTE_COLUMNS)):
        produced = by_id[stage_id].output_schema
        assert produced is not None
        assert TableSchema(columns=list(wanted)).find_unsatisfied_columns(produced) == []


def test_the_bundle_reads_every_filing_it_names_from_the_committed_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a mirror read reached for the network")

    monkeypatch.setattr(connector, "urlopen", refuse)
    params = RecapDocketParams.model_validate(_read_input_params())
    acquired = list(acquire_recap_docket(params))
    assert [file.metadata["ecf_entry"] for file in acquired] == [
        int(number.split("-")[0]) for number in params.entries]
    for file in acquired:
        assert isinstance(file, MirroredBytes)
        with file.open_bytes() as stream:
            assert hashlib.sha256(stream.read()).hexdigest() == file.sha256


def test_the_committed_eval_scores_classify_event_on_its_twelve_rows() -> None:
    project_id = _import_bundle()
    config = EvalConfig.model_validate(
        {**json.loads(_EVAL.read_text(encoding="utf-8")), "project": project_id})
    report = validate_eval_compatibility(config, _load_imported_workflow(project_id))
    assert report.ok, report.problems
    assert report.settings is not None
    assert report.settings.frontier == ["classify_event"]
    assert config.table is not None
    assert len(read_table_ref(config.table)) == 12
