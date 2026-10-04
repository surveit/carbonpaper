"""The Boeing docket chronology bundle imports, publishes the shared tables, and reads offline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.evals.compatibility import validate_eval_compatibility
from app.evals.dataset import read_table_ref
from app.models import Column, TableSchema, Workflow
from app.models.connectors import MirroredBytes
from app.models.records.eval_config import EvalConfig
from app.packs.docket import connector
from app.packs.docket.connector import RecapDocketParams, acquire_recap_docket
from app.main import app
from app.core.stage_cache import StageCacheEntry
from app.models.run_manifest import QUOTE_REFUSAL_ERROR_TYPE
from app.runtime.run_log import JUDGMENT_ID, ROW_OK, SOURCE_CACHED, read_events_since
from app.runtime.stages import llm_transform
from app.services import run as run_service, versioning, workflow_summary
from app.services.project import WorkflowFile, import_bundle_file, import_project

_SEEDS = Path(__file__).resolve().parents[1] / "app" / "seeds" / "data"
_BUNDLE = _SEEDS / "boeing_docket_chronology.json"
_EVAL = _SEEDS / "evals" / "boeing_docket_chronology.json"

# What a chronology view of any pack would read: speaker and topic stay open text.
_EVENT_COLUMNS = [
    Column(name="date", type="str", nullable=True),
    Column(name="date_precision", type="str", nullable=False,
           enum=["day", "month", "year", "none"]),
    Column(name="speaker", type="str", nullable=False),
    Column(name="topic", type="str", nullable=False),
    Column(name="statement", type="str", nullable=False),
    Column(name="span", type="span", nullable=False),
    Column(name="pin_cite", type="str", nullable=False),
    Column(name="source_kind", type="str", nullable=False),
]
_DISPUTE_COLUMNS = [
    Column(name="topic", type="str", nullable=False),
    Column(name="status", type="str", nullable=False,
           enum=["undisputed", "disputed", "unreviewed"]),
    Column(name="summary", type="str", nullable=False),
    Column(name="supporting", type="list[span]", nullable=False),
    Column(name="contrary", type="list[span]", nullable=False),
]

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
    for stage_id, wanted in (("chronology", _EVENT_COLUMNS), ("disputes", _DISPUTE_COLUMNS)):
        produced = by_id[stage_id].output_schema
        assert produced is not None
        assert TableSchema(columns=wanted).find_unsatisfied_columns(produced) == []


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


def _refuse_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a replay of the committed cache called the model")

    monkeypatch.setattr(llm_transform, "call_llm", refuse)
    monkeypatch.setattr(llm_transform, "call_llm_batch", refuse)


def test_the_committed_cache_replays_the_bundle_with_no_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_model_calls(monkeypatch)
    project_id = import_bundle_file(_BUNDLE).project_id

    manifest = run_service.execute(project_id)

    assert manifest["status"] == "awaiting_review"
    by_stage = {record["stage_id"]: record for record in manifest["stage_records"]}
    for stage_id in ("extract_events", "classify_event", "quote_event", "find_disputes"):
        assert by_stage[stage_id]["cached_rows"] == by_stage[stage_id]["output_row_count"] > 0
    replayed = [event for event in read_events_since(project_id, manifest["run_id"], 0)
                if event.get("stage") == "quote_event" and event["kind"] == ROW_OK]
    assert {event["source"] for event in replayed} == {SOURCE_CACHED}
    page = TestClient(app).get(f"/project/{project_id}/judgments/{replayed[0][JUDGMENT_ID]}")
    assert page.status_code == 200, page.text


def test_a_replayed_quote_is_checked_against_its_page_like_a_computed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_model_calls(monkeypatch)
    project_id = import_bundle_file(_BUNDLE).project_id
    entry = next(entry for entry in StageCacheEntry.read_only().find_project_entries(project_id)
                 if entry.stage_id == "quote_event" and entry.output_row is not None)
    assert entry.output_row is not None
    span = entry.output_row["span"]
    assert isinstance(span, dict) and isinstance(span["quote"], str)
    # Same length, so the span still parses: only the page can refuse it.
    moved = {**span, "quote": "x" * len(span["quote"])}
    entry.model_copy(update={"output_row": {**entry.output_row, "span": moved}}).save()

    manifest = run_service.execute(project_id)

    quote_event = next(r for r in manifest["stage_records"] if r["stage_id"] == "quote_event")
    assert quote_event["error"]["type"] == QUOTE_REFUSAL_ERROR_TYPE
    assert "x" * 20 in quote_event["error"]["message"]
