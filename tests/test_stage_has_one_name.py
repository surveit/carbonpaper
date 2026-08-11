"""A stage has ONE name — its id — and `description` is the line under it.

Pins the two ceilings that keep them in their roles, and the surfaces that must
show the id rather than the prose.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage
from app.models.stage import StageDraft
from app.models.stages.stage_base import (
    STAGE_DESCRIPTION_MAX_CHARS,
    STAGE_ID_MAX_CHARS,
)
from app.runtime.manifest import RunManifest
from app.web.diagrams import build_mermaid_graph

_COLUMNS = [{"name": "id", "type": "str", "nullable": True}]
_SPEC = {
    "id": "load_roster", "description": "Roster snapshot — 2026-07-15",
    "type": "input_data",
    "connector": {"kind": "file", "params": {"path": "roster.csv"}},
    "signature": {"produces": _COLUMNS},
}


def _spec(**overrides: object) -> dict[str, object]:
    return {**_SPEC, **overrides}


def test_a_stage_without_a_description_is_refused():
    spec = _spec()
    del spec["description"]
    with pytest.raises(ValidationError, match="description"):
        parse_stage(spec)


def test_an_id_over_the_ceiling_is_refused():
    with pytest.raises(ValidationError, match=f"at most {STAGE_ID_MAX_CHARS} characters"):
        parse_stage(_spec(id="a" * (STAGE_ID_MAX_CHARS + 1)))


def test_a_description_over_the_ceiling_is_refused():
    with pytest.raises(
        ValidationError, match=f"at most {STAGE_DESCRIPTION_MAX_CHARS} characters"
    ):
        parse_stage(_spec(description="d" * (STAGE_DESCRIPTION_MAX_CHARS + 1)))


def test_both_ceilings_bind_the_draft_an_authoring_client_submits():
    with pytest.raises(ValidationError):
        StageDraft.model_validate(_spec(id="a" * (STAGE_ID_MAX_CHARS + 1)))
    with pytest.raises(ValidationError):
        StageDraft.model_validate(
            _spec(description="d" * (STAGE_DESCRIPTION_MAX_CHARS + 1))
        )


def test_the_authoring_schema_carries_both_ceilings_to_the_agent():
    properties = StageDraft.model_json_schema()["properties"]
    assert properties["id"]["maxLength"] == STAGE_ID_MAX_CHARS
    assert properties["description"]["maxLength"] == STAGE_DESCRIPTION_MAX_CHARS
    assert "ONE name" in properties["id"]["description"]
    assert "ONE line" in properties["description"]["description"]


def test_the_graph_labels_the_node_with_the_id_and_hovers_the_description():
    graph = build_mermaid_graph([_spec()], "demo")
    assert '<b>⬆️ load_roster</b>' in graph
    assert 'Roster snapshot — 2026-07-15"' in graph
    assert '<b>⬆️ Roster snapshot' not in graph


def test_a_description_holding_a_quote_cannot_end_the_mermaid_tooltip_early():
    graph = build_mermaid_graph([_spec(description='the "flagged" rows')], "demo")
    assert """dvNode("load_roster") "the 'flagged' rows\"""" in graph


def test_a_manifest_written_before_the_rename_still_loads():
    legacy = """{"run_id": "r1", "started_at": "2026-01-01T00:00:00",
      "project": "demo", "workflow_version": null, "limit_overrides": {},
      "offset_overrides": {}, "run_bindings": {}, "input_bindings": {},
      "human_review_queue_stats": {}, "dropped_columns": {}, "status": "ok",
      "stage_records": [{"stage_id": "load_roster", "type": "input_data",
        "name": "Roster snapshot", "started_at": "2026-01-01T00:00:00",
        "status": "ok", "input_validation_report": [],
        "output_validation_report": null, "elapsed_ms": 5, "output_row_count": 2}],
      "finished_at": "2026-01-01T00:00:01"}"""
    manifest = RunManifest.model_validate_json(legacy)
    assert manifest.stage_records[0].stage_id == "load_roster"
    assert not hasattr(manifest.stage_records[0], "name")
