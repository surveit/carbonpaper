"""What the figure card claims, refuses, and will not guess. docs/figure-card.md"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services.project import save_working_copy_as_version
from app.models.claims import StageOutputCellCitation
from app.web.figure_card import (
    FigureCard,
    FigureReceipt,
    NoReceipt,
    Receipt,
    Step,
    StepsByHand,
    describe_figure_for_a_link_preview,
    load_figure_card,
)
from scope_fixture import publish_tail, stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "figure_card_fixture"
# grant_totals row 0: the five grants left after the drops below (scope_fixture).
CARD = "/figure/{project}/{run}/grant_totals/0/total_amount"


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data) + publish_tail())
    save_working_copy_as_version(PROJECT, message="fixture", reviewer="test")
    return str(run_service.execute(PROJECT)["run_id"])


def read_card(run_id: str):
    return TestClient(app).get(CARD.format(project=PROJECT, run=run_id))


def test_the_card_leads_with_the_label_the_publish_stage_gave_the_figure(run_id):
    page = read_card(run_id)
    assert page.status_code == 200
    assert "What the grants come to" in page.text


def test_the_value_is_the_cell_the_publish_stage_recorded(run_id):
    assert "2200" in read_card(run_id).text


def test_a_cell_no_publish_stage_cited_is_not_a_figure(run_id):
    refused = TestClient(app).get(
        f"/figure/{PROJECT}/{run_id}/grant_totals/0/grants")
    assert refused.status_code == 404
    assert "not a published figure" in refused.json()["detail"]


def test_the_receipt_counts_the_rows_the_figure_was_counted_from(run_id):
    card = _load(run_id)
    # Ten filings, less one recorded at zero, one filed twice, and three loans.
    assert card.receipt.counted_from_rows == 5
    assert card.receipt.counted_from_stage == "grants_only"


def test_the_receipt_names_the_widest_frame_the_figure_came_through(run_id):
    card = _load(run_id)
    assert card.receipt.read_from_rows == 10
    assert card.receipt.read_from_stage == "both_regions"


def test_the_receipt_counts_the_rows_the_run_took_out(run_id):
    taken = {cut.at_stage: cut.rows for cut in _load(run_id).receipt.taken_out}
    # One zero-amount grant, one duplicate filing, and three loans.
    assert taken == {"funded": 1, "one_row_per_grant": 1, "grants_only": 3}


def test_a_run_with_no_model_and_no_reviewer_says_so_rather_than_leaving_it_open(run_id):
    steps = _load(run_id).receipt.steps
    assert steps.judged_by_a_model == []
    assert steps.signed_off_by_a_person == []
    assert [step.stage_id for step in steps.ran_as_code]


def test_the_lookup_table_is_named_apart_from_the_steps_that_counted(run_id):
    card = _load(run_id)
    assert card.receipt.reference_tables == ["load_agencies"]
    assert "load_agencies" not in [step.stage_id for step in card.receipt.steps.ran_as_code]


def test_the_link_preview_counts_off_the_receipt(run_id):
    preview = describe_figure_for_a_link_preview(_load(run_id))
    assert "Counted from 5 rows of 10 read from source" in preview
    assert "0 a model judged and 0 a person signed off" in preview


def test_the_page_carries_the_link_preview_metadata(run_id):
    page = read_card(run_id).text
    assert 'property="og:title"' in page
    assert 'property="og:description"' in page
    assert 'property="og:url"' in page
    assert 'name="twitter:card"' in page


def test_no_image_is_offered_for_a_preview_the_app_cannot_draw(run_id):
    assert 'property="og:image"' not in read_card(run_id).text


def test_the_run_that_cannot_show_its_rows_says_why_and_counts_nothing():
    preview = describe_figure_for_a_link_preview(
        _card_with(NoReceipt(reason="it wrote no lineage sidecar")))
    assert "This run did not record which rows the figure came from." in preview
    assert "Counted from" not in preview


def test_the_link_preview_names_a_model_step_where_one_is_on_the_route():
    steps = StepsByHand(
        ran_as_code=[Step(stage_id="load", type_label="input", description="")],
        judged_by_a_model=[Step(stage_id="read_it", type_label="model transform",
                                description="")],
        signed_off_by_a_person=[])
    card = _card_with(_receipt_with(steps))
    assert "1 a model judged and 0 a person signed off" in (
        describe_figure_for_a_link_preview(card))


def _load(run_id: str) -> FigureCard:
    card = load_figure_card(PROJECT, run_id, StageOutputCellCitation(
        run_id=run_id, stage_id="grant_totals", row_ordinal=0,
        column="total_amount", value=None))
    assert card is not None
    return card


def _receipt_with(steps: StepsByHand) -> FigureReceipt:
    return FigureReceipt(
        counted_from_stage="grants_only", counted_from_rows=5,
        read_from_stage="both_regions", read_from_rows=10, taken_out=[],
        unrecorded_rows=0, steps=steps, reference_tables=[],
        rows_href="/rows", trace_href="/trace")


def _card_with(receipt: Receipt) -> FigureCard:
    return FigureCard(
        project_id=PROJECT, run_id="R1", label="What the grants come to", value="2200",
        project_name=PROJECT, document=None, counted_at="2026-08-26T00:00:00",
        version_id="V1", receipt=receipt, run_href="/run")
