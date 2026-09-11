"""The claims list: every claim a project has made, what is waiting, and the nav leaf."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.claims import ClaimImportance, ClaimShapeInput, DataUniverseRequirement
from app.models.records.claims import Claim
from app.models.records.workflow_output import WorkflowOutput
from app.models.schema import Column
from app.services import claim_shapes
from app.services import claims as claims_service
from app.web.claims_list_view import build_claims_list_page
from claim_review_fixture import PROJECT, TOTAL_TEXT, claim_the_total, run_the_fixture

COUNT_TEXT = "Five grants were recorded."
COUNT_SHAPE = ClaimShapeInput(
    label="How many grants were recorded",
    universe=DataUniverseRequirement.open, importance=ClaimImportance.secondary,
    context=[Column(name="period", type="str", nullable=False)])


@pytest.fixture
def client() -> Any:
    with TestClient(app, follow_redirects=False) as running:
        yield running


@pytest.fixture
def two_claims(projects_root) -> tuple[str, Claim, Claim]:
    """The total, claimed first, then the count — so newest-first and status disagree."""
    run_id = run_the_fixture(projects_root)
    total = claim_the_total(run_id)
    return run_id, total, claim_the_count(run_id, COUNT_TEXT)


def claim_the_count(run_id: str, text: str) -> Claim:
    shape = _write_the_count_shape()
    [output] = [one for one in WorkflowOutput.list() if one.slug == "grant-count"]
    output.shape_id = shape.id
    output.save()
    return claims_service.submit_claim(
        PROJECT, run_id, "grant-count", {"period": "2024"}, text)


def _write_the_count_shape() -> Any:
    written = claim_shapes.write_claim_shapes(PROJECT, [COUNT_SHAPE])
    [shape] = [one for one in written if one.label == COUNT_SHAPE.label]
    return shape


def read_the_list(client: TestClient) -> Any:
    return client.get(f"/project/{PROJECT}/claims")


# ── the order the rows run in ─────


def test_a_claim_waiting_on_review_lists_before_one_already_made(two_claims):
    _, total, count = two_claims
    claims_service.approve_claim(PROJECT, count.id, True)

    page = build_claims_list_page(PROJECT)

    assert [row.claim_id for row in page.rows] == [total.id, count.id]
    assert [row.status_words for row in page.rows] == ["needs review", "approved"]


def test_two_claims_in_the_same_state_list_newest_first(two_claims):
    _, total, count = two_claims

    page = build_claims_list_page(PROJECT)

    assert [row.claim_id for row in page.rows] == [count.id, total.id]


# ── what each row says ─────


def test_a_row_carries_the_sentence_its_value_and_where_it_was_read(two_claims):
    _, total, _ = two_claims

    [row] = [one for one in build_claims_list_page(PROJECT).rows if one.claim_id == total.id]

    assert row.text == TOTAL_TEXT
    assert row.value == "2200"      # render_figure, as the claim page reads it
    assert row.shape_label == "What the grants came to, in whole units"
    assert row.stage_id == "grant_totals"
    assert row.run_id == total.citation.run_id
    assert row.href == f"/project/{PROJECT}/claims/{total.id}"
    assert row.run_href == f"/project/{PROJECT}/runs/{total.citation.run_id}"


def test_a_row_spells_out_the_context_the_claim_sits_on(two_claims):
    _, _, count = two_claims

    [row] = [one for one in build_claims_list_page(PROJECT).rows if one.claim_id == count.id]

    assert row.context_words == "period 2024"


def test_a_skipped_output_lists_as_declined_under_its_metric_label(projects_root):
    run_id = run_the_fixture(projects_root)
    claim_the_total(run_id)
    skipped = claims_service.decline_output(PROJECT, run_id, "grant-total")

    [row] = [one for one in build_claims_list_page(PROJECT).rows if one.claim_id == skipped.id]

    assert row.status_words == "declined"
    assert row.text == "What the grants came to, in whole units"


# ── the tally ─────


def test_the_tally_counts_what_is_waiting_and_what_was_made(two_claims):
    _, _, count = two_claims
    claims_service.approve_claim(PROJECT, count.id, True)

    page = build_claims_list_page(PROJECT)

    assert (page.to_review, page.made, page.declined, page.superseded) == (1, 1, 0, 0)


def test_the_tally_line_writes_no_count_of_zero(two_claims, client):
    _, _, count = two_claims
    claims_service.approve_claim(PROJECT, count.id, True)

    body = read_the_list(client).text

    assert "<b>1</b> to review" in body and "<b>1</b> made" in body
    assert "declined" not in body and "superseded" not in body


# ── the page itself ─────


def test_the_page_lists_every_claim_with_what_waits_on_review_written_first(
    two_claims, client
):
    _, _, count = two_claims
    claims_service.approve_claim(PROJECT, count.id, True)

    response = read_the_list(client)

    assert response.status_code == 200
    assert TOTAL_TEXT in response.text and COUNT_TEXT in response.text
    assert response.text.index(TOTAL_TEXT) < response.text.index(COUNT_TEXT)


def test_a_project_with_no_claims_offers_the_run_they_would_be_written_on(
    projects_root, client
):
    run_id = run_the_fixture(projects_root)

    body = read_the_list(client).text

    assert "Nothing claimed yet" in body
    assert f'href="/project/{PROJECT}/runs/{run_id}/publish"' in body


def test_a_project_with_no_run_states_the_absence_and_offers_no_button(
    projects_root, client
):
    (projects_root / PROJECT).mkdir(parents=True, exist_ok=True)

    body = read_the_list(client).text

    assert "Nothing claimed yet" in body
    assert "btn primary" not in body


# ── the nav leaf ─────


def test_the_nav_on_any_project_page_carries_claims(two_claims, client):
    body = client.get(f"/project/{PROJECT}/runs").text

    assert f'href="/project/{PROJECT}/claims"' in body
    assert '<span class="app-nav-label">Claims</span>' in body


def test_the_claims_leaf_carries_no_count(two_claims):
    """The nav is a table of contents; what is waiting is stated on the page itself."""
    from app.web.project_view import build_nav

    [leaf] = [item for item in build_nav(PROJECT) if item.key == "claims"]

    assert leaf.model_dump() == {
        "key": "claims", "label": "Claims", "href": f"/project/{PROJECT}/claims",
        "children": []}
