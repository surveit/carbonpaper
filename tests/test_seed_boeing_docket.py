"""The Boeing docket chronology bundle imports, publishes the shared tables, and reads offline."""
from __future__ import annotations

import hashlib
import json

import pytest

from app.models import TableSchema, Workflow
from app.models.chronology import DISPUTE_COLUMNS, EVENT_COLUMNS
from app.models.connectors import MirroredBytes
from app.packs.docket import connector
from app.packs.docket.connector import RecapDocketParams, acquire_recap_docket
from app.packs.docket.tour import BOEING_TOUR
from app.services import versioning, workflow_summary
from app.services.project import WorkflowFile, import_project


def _import_bundle() -> str:
    return import_project(
        WorkflowFile.model_validate_json(BOEING_TOUR.bundle.read_text(encoding="utf-8")))


def _read_input_params() -> dict[str, object]:
    bundle = json.loads(BOEING_TOUR.bundle.read_text(encoding="utf-8"))
    (stage,) = [stage for stage in bundle["stages"] if stage["id"] == "input_filings"]
    return stage["connector"]["params"]


def test_the_bundle_imports_with_no_issues() -> None:
    project_id = _import_bundle()
    assert workflow_summary.read_workflow_summary(project_id).issues == []


def test_the_published_tables_hold_the_shared_chronology_columns() -> None:
    project_id = _import_bundle()
    version_id = versioning.find_latest_version_id(project_id)
    assert version_id is not None
    stages = Workflow(stages=versioning.load_version_stages(project_id, version_id))
    by_id = stages.index_workflow_stages_by_id()
    for stage_id, wanted in (("chronology", EVENT_COLUMNS), ("disputes", DISPUTE_COLUMNS)):
        produced = by_id[stage_id].output_schema
        assert produced is not None
        assert TableSchema(columns=list(wanted)).find_unsatisfied_columns(produced) == []


def test_the_committed_mirror_serves_every_filing_the_bundle_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a mirror read reached for the network")

    monkeypatch.setattr(connector, "urlopen", refuse)
    params = RecapDocketParams.model_validate(
        {**_read_input_params(), **BOEING_TOUR.bindings["input_filings"]})
    acquired = list(acquire_recap_docket(params))
    assert [file.metadata["ecf_entry"] for file in acquired] == [
        int(number.split("-")[0]) for number in params.entries]
    for file in acquired:
        assert isinstance(file, MirroredBytes)
        with file.open_bytes() as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == file.sha256
