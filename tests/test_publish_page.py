"""The publish page: which outputs it offers, what the buttons do, and what it counts."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.run_status import RunStatus
from app.main import app
from app.models.claims import (
    ClaimImportance,
    ClaimShapeInput,
    DataUniverseRequirement,
    StageOutputCellCitation,
)
from app.models.records.claims import Claim
from app.models.records.run_manifest import RunManifest
from app.models.records.workflow_output import WorkflowOutput
from app.models.run_parameters import RunParameters
from app.models.schema import Column
from app.services import claim_shapes, workspace
from app.services.methodology import write_methodology

_PROJECT = "ai_lobbying"
_RUN = "20260901T103753.789399"
_BASE = f"/project/{_PROJECT}/runs/{_RUN}"
_SPEND = ClaimShapeInput(
    label="US lobbying spend on AI, reported by outside firms",
    requires=DataUniverseRequirement.closed, importance=ClaimImportance.primary,
    template="Outside firms reported ${value} in AI lobbying income.",
    context=[
        Column(name="period_start", type="date", nullable=False),
        Column(name="period_end", type="date", nullable=False),
    ],
    qualifiers=["Counts only lobbying that was filed."],
)
_SENTENCE = "Outside firms reported $63,027,729 in AI lobbying income in H1 2026."
_FORM = {
    "text": _SENTENCE,
    "context.period_start": "2026-01-01",
    "context.period_end": "2026-06-30",
}


def _a_run(tmp_path, parameters: RunParameters | None = None) -> TestClient:
    workspace.set_projects_dir(tmp_path)
    (tmp_path / _PROJECT).mkdir()
    write_methodology(_PROJECT, "Follow the filings.")
    RunManifest(
        id=RunManifest.compose_id(_PROJECT, _RUN), run_id=_RUN,
        started_at="2026-09-01T10:37:53", project=_PROJECT,
        workflow_version="20260901T103742.393151", parameters=parameters or RunParameters(),
        input_bindings={}, human_review_queue_stats={}, dropped_columns={},
        status=RunStatus.OK, stage_records=[],
    ).save()
    [shape] = claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])
    for slug, value, shape_id in (
        ("ai-spend", 63027729.0, shape.id), ("corpus-rows", 45061, None)
    ):
        WorkflowOutput(
            slug=slug, label=slug, primary=True, shape_id=shape_id,
            citation=StageOutputCellCitation(
                run_id=_RUN, stage_id="ai_spend_totals", row_ordinal=0,
                column="ai_spend", value=value,
            ),
        ).save()
    return TestClient(app, follow_redirects=False)


def test_only_an_output_that_names_a_shape_is_on_the_page(tmp_path):
    page = _a_run(tmp_path).get(f"{_BASE}/publish").text

    assert "63,027,729.0" in page
    assert "45,061" not in page                       # names no shape, so it claims nothing
    assert "US lobbying spend on AI" in page
    assert "Counts only lobbying that was filed." in page


def test_the_suggested_sentence_arrives_filled_in_not_as_a_template(tmp_path):
    page = _a_run(tmp_path).get(f"{_BASE}/publish").text

    assert "Outside firms reported 63,027,729.0 in AI lobbying income." in page
    assert "${value}" not in page
    assert 'name="context.period_start"' in page
    assert 'type="date"' in page


def test_the_context_pre_fills_from_the_newest_standing_claim(tmp_path):
    client = _a_run(tmp_path)
    client.post(f"{_BASE}/submit/ai-spend", data=_FORM)
    [claim] = Claim.find(created_by_project_id=_PROJECT)
    client.post(f"{_BASE}/approve/{claim.id}")
    WorkflowOutput(
        slug="ai-spend", label="ai-spend", primary=True, shape_id=claim.shape_id,
        citation=StageOutputCellCitation(
            run_id="20260902T090000.000000", stage_id="ai_spend_totals", row_ordinal=0,
            column="ai_spend", value=71000000.0,
        ),
    ).save()
    RunManifest(
        id=RunManifest.compose_id(_PROJECT, "20260902T090000.000000"),
        run_id="20260902T090000.000000", started_at="2026-09-02T09:00:00", project=_PROJECT,
        workflow_version="20260901T103742.393151", parameters=RunParameters(),
        input_bindings={}, human_review_queue_stats={}, dropped_columns={},
        status=RunStatus.OK, stage_records=[],
    ).save()

    page = client.get(f"/project/{_PROJECT}/runs/20260902T090000.000000/publish").text

    assert 'value="2026-01-01"' in page   # the period the standing claim used
    assert "nothing — the same claim" in page


def test_submitting_writes_the_sentence_and_puts_it_in_review(tmp_path):
    client = _a_run(tmp_path)

    response = client.post(f"{_BASE}/submit/ai-spend", data=_FORM)

    assert response.status_code == 303
    [claim] = Claim.find(created_by_project_id=_PROJECT)
    assert (claim.text, claim.status) == (_SENTENCE, "submitted")
    assert claim.context["period_start"] == "2026-01-01"


def test_a_claim_with_no_sentence_is_refused(tmp_path):
    client = _a_run(tmp_path)

    response = client.post(f"{_BASE}/submit/ai-spend", data={**_FORM, "text": "  "})

    assert response.status_code == 400
    assert "write it" in response.text


def test_approving_is_what_makes_the_claim_stand(tmp_path):
    client = _a_run(tmp_path)
    client.post(f"{_BASE}/submit/ai-spend", data=_FORM)
    [claim] = Claim.find(created_by_project_id=_PROJECT)

    response = client.post(f"{_BASE}/approve/{claim.id}")

    assert response.status_code == 303
    assert Claim.load(claim.id).status == "approved"


def test_a_closed_metric_cannot_stand_on_a_capped_run(tmp_path):
    client = _a_run(tmp_path, RunParameters(limits={"read_filings": 5000}))
    page = client.get(f"{_BASE}/publish").text

    assert "read_filings (first 5,000)" in page
    assert "turns &#39;is&#39; into &#39;at least&#39;" in page


def test_skipping_leaves_a_declined_claim_and_the_counts_say_so(tmp_path):
    client = _a_run(tmp_path)

    client.post(f"{_BASE}/skip/ai-spend")

    assert [c.status for c in Claim.find(created_by_project_id=_PROJECT)] == ["declined"]
    assert "skipped" in client.get(f"{_BASE}/publish").text
