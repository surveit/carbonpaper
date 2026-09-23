"""The claim page: the sentence banded by what was raised, and the state of its review."""
from __future__ import annotations

from typing import Any

import re

import pytest
from fastapi.testclient import TestClient

from app.core.agent.store import SessionStore
from app.main import app
from app.models.citations import (
    RowsRectangle,
    StageCitation,
    StageOutputCellCitation,
    TermCitation,
    address_citation,
    StageOutputColumnCitation,
    StageOutputTableCitation,
)
from app.models.records.claim_review import (
    SEVERITY_WORDS,
    ChallengeKind,
    ClaimPart,
    ClaimReview,
    DraftChallenge,
    Severity,
)
from app.models.records.claims import Claim
from app.models.records.workflow_output import WorkflowOutput
from app.reviewer.run import PARENT_ROLE
from app.services import claims as claims_service
from app.services.claim_review import store_claim_review
from app.services.generation import GENERATION_FAILURE_PREFIX
from app.web.citation_links import render_source_url
from app.web.claim_review_view import build_claim_review_page
from claim_review_fixture import (
    PROJECT,
    TOTAL_SHAPE,
    TOTAL_TEXT,
    claim_the_total,
    publish_the_outputs,
    run_the_fixture,
)

_FIGURE = "2,200"
_WHOLE = "in total"


@pytest.fixture
def claim(projects_root) -> Claim:
    return claim_the_total(run_the_fixture(projects_root))


def _challenge(claim: Claim, phrase: str | None, *, severity: Severity,
               kind: ChallengeKind = ChallengeKind.data,
               citations: list | None = None) -> DraftChallenge:
    return DraftChallenge(
        kind=kind,
        claim_part=ClaimPart(phrase=phrase) if phrase is not None else None,
        text=f"what {phrase!r} says is not what the run counted",
        justification="the amount column is blank on every row",
        citations=citations or [StageOutputColumnCitation(
            run_id=claim.citation.run_id, stage_id="grant_totals", column="grants")],
        severity=severity)


def store_a_review(claim: Claim, *challenges: DraftChallenge) -> ClaimReview:
    return store_claim_review(PROJECT, claim.id, challenges=list(challenges),
                              session_id="session-review")


def open_a_session(claim_id: str) -> tuple[SessionStore, str]:
    store = SessionStore()
    session_id = store.create(
        title="Review", agent_id=None,
        context={"role": PARENT_ROLE, "claim_id": claim_id, "project_id": PROJECT})
    store.set_active_turn(session_id, "review")
    return store, session_id


def fail_the_turn(store: SessionStore, session_id: str, error: str) -> None:
    store.set_active_turn(session_id, None)
    store.append_messages(session_id, [{
        "role": "assistant",
        "parts": [{"type": "text", "text": f"{GENERATION_FAILURE_PREFIX}{error}"}]}])


# ── the sentence ─────


def test_a_claim_with_no_review_reads_as_one_plain_sentence(claim):
    page = build_claim_review_page(PROJECT, claim.id)

    assert [token.text for token in page.tokens] == [TOTAL_TEXT]
    assert [token.severity for token in page.tokens] == [None]
    assert page.review == "none"


def test_a_stored_review_bands_the_phrase_its_challenge_lands_on(claim):
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.high))

    page = build_claim_review_page(PROJECT, claim.id)

    banded = [token for token in page.tokens if token.severity is not None]
    assert [token.text for token in banded] == [_FIGURE]
    assert banded[0].severity == Severity.high
    assert "".join(token.text for token in page.tokens) == TOTAL_TEXT


def test_two_challenges_on_overlapping_phrases_cut_the_sentence_at_every_edge(claim):
    store_a_review(
        claim,
        _challenge(claim, "Grants came", severity=Severity.low),
        _challenge(claim, "came to", severity=Severity.critical))

    page = build_claim_review_page(PROJECT, claim.id)

    # The overlap is banded by the worse of the two, and no character is drawn twice.
    assert "".join(token.text for token in page.tokens) == TOTAL_TEXT
    overlap = [token for token in page.tokens if token.text == "came"]
    assert [token.severity for token in overlap] == [Severity.critical]


def test_a_challenge_about_the_whole_sentence_bands_nothing(claim):
    store_a_review(claim, _challenge(claim, None, severity=Severity.critical))

    page = build_claim_review_page(PROJECT, claim.id)

    assert [token.severity for token in page.tokens] == [None]
    assert page.challenges[0].phrase == ""


# ── what was raised ─────


def test_every_challenge_is_in_one_list_worst_first(claim):
    store_a_review(
        claim,
        _challenge(claim, _WHOLE, severity=Severity.info),
        _challenge(claim, _FIGURE, severity=Severity.low),
        _challenge(claim, "Grants", severity=Severity.critical))

    page = build_claim_review_page(PROJECT, claim.id)

    assert [one.severity for one in page.challenges] == [
        Severity.critical, Severity.low, Severity.info]


def test_a_banded_phrase_opens_the_challenge_it_carries(claim):
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.high))

    page = build_claim_review_page(PROJECT, claim.id)

    [banded] = [token for token in page.tokens if token.severity is not None]
    assert banded.anchor == page.challenges[0].anchor


def test_a_card_names_its_weight_and_carries_the_rubric_line(claim):
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.high,
                                     kind=ChallengeKind.coverage))

    [card] = build_claim_review_page(PROJECT, claim.id).challenges

    assert card.severity_name == "high"
    assert card.severity_words == SEVERITY_WORDS[Severity.high]


@pytest.mark.parametrize("citation", [
    StageOutputCellCitation(run_id="r", stage_id="grant_totals", row_ordinal=0,
                            column="total_amount", value=2200),
    StageOutputColumnCitation(run_id="r", stage_id="grant_totals", column="grants"),
    StageCitation(stage_id="grant_totals"),
    TermCitation(name="grant"),
], ids=lambda one: one.kind)
def test_every_citation_kind_opens_a_page_the_app_serves(citation):
    """A citation the reader cannot open is the one thing a citation may not be."""
    url = render_source_url(address_citation(PROJECT, citation))

    assert _find_get_route(url) is not None, f"{url} is on no GET route"


def _find_get_route(url: str) -> str | None:
    """Matched against the served path table, so a URL on no GET route is a dead link."""
    path = url.split("#")[0].split("?")[0]
    for template, methods in app.openapi()["paths"].items():
        if "get" in methods and re.fullmatch(_as_pattern(template), path):
            return template
    return None


def _as_pattern(template: str) -> str:
    return re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template))


def test_a_citation_reads_as_a_trail_and_a_cell_one_carries_its_value(claim):
    cell = StageOutputCellCitation(run_id=claim.citation.run_id, stage_id="grant_totals",
                                   row_ordinal=0, column="total_amount", value=2200)
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.high,
                                     citations=[cell]))

    [card] = build_claim_review_page(PROJECT, claim.id).challenges

    [cited] = card.citations
    assert cited.trail == ["grant_totals", "total_amount", "row 0"]
    assert cited.value == "2200"
    assert cited.href.startswith(f"/project/{PROJECT}/")


def test_a_citation_that_names_no_single_value_shows_none(claim):
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.high))

    [card] = build_claim_review_page(PROJECT, claim.id).challenges

    [cited] = card.citations
    assert cited.trail == ["grant_totals", "grants"] and cited.value == ""


def test_the_legend_is_the_rubric_itself_worst_first(claim):
    legend = build_claim_review_page(PROJECT, claim.id).legend

    assert [row.severity for row in legend] == [3, 2, 1, 0]
    assert [row.words for row in legend] == [
        SEVERITY_WORDS[Severity(weight)] for weight in (3, 2, 1, 0)]


# ── what the claim sits on ─────


def test_the_page_carries_what_the_run_read_and_what_blocks_it(claim):
    page = build_claim_review_page(PROJECT, claim.id)

    assert page.text == TOTAL_TEXT
    assert page.run_id == claim.citation.run_id
    assert page.status == "submitted"
    assert page.run_read_everything is True


def test_the_cited_figure_is_linked_the_same_way_wherever_the_page_draws_it(claim):
    page = build_claim_review_page(PROJECT, claim.id)

    [cited] = [row for row in page.outputs if row.cited]
    assert cited.href == page.value_href


# ── the review behind it ─────


def test_a_review_with_a_turn_in_flight_reads_as_running(claim):
    _store, session_id = open_a_session(claim.id)

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.review == "running"
    assert page.review_session_id == session_id


def test_a_review_that_failed_says_what_it_said(claim):
    store, session_id = open_a_session(claim.id)
    fail_the_turn(store, session_id, "the data reviewer submitted nothing")

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.review == "failed"
    assert "submitted nothing" in (page.review_error or "")


def test_a_session_on_another_claim_says_nothing_about_this_one(claim):
    open_a_session("another-claim")

    assert build_claim_review_page(PROJECT, claim.id).review == "none"


def test_a_stored_review_reads_as_done_and_names_its_session(claim):
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.high))

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.review == "done"
    assert page.review_session_id == "session-review"


def test_a_table_claim_is_refused_rather_than_reviewed(projects_root):
    run_id = run_the_fixture(projects_root)
    shape = publish_the_outputs(run_id)
    _publish_a_table(run_id, shape.id)
    claim = claims_service.submit_claim(
        PROJECT, run_id, "grant-rows", {}, "Five grants were recorded.")

    assert build_claim_review_page(PROJECT, claim.id).review == "refused"


def _publish_a_table(run_id: str, shape_id: str) -> None:
    WorkflowOutput(
        slug="grant-rows", label="The grants themselves", primary=False, shape_id=shape_id,
        citation=StageOutputTableCitation(
            run_id=run_id, stage_id="grant_totals",
            rectangle=RowsRectangle(row_start=0, row_end=5, columns=["amount"])),
    ).save()


# ── the route ─────


@pytest.fixture
def client() -> Any:
    with TestClient(app, follow_redirects=False) as running:
        yield running


def read_the_page(client: TestClient, claim_id: str) -> Any:
    return client.get(f"/project/{PROJECT}/claims/{claim_id}")


def test_a_claim_nobody_has_read_offers_the_review(claim, client):
    page = read_the_page(client, claim.id)

    assert page.status_code == 200
    assert "Review this claim" in page.text


def test_the_page_draws_a_stored_review(claim, client):
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.critical))

    page = read_the_page(client, claim.id)

    assert page.status_code == 200
    assert SEVERITY_WORDS[Severity.critical] in page.text


def test_a_claim_this_project_does_not_hold_is_a_404(client, projects_root):
    assert read_the_page(client, "no-such-claim").status_code == 404


def test_the_trail_runs_through_claims_to_the_sentence(claim, client):
    trail = read_the_trail(read_the_page(client, claim.id).text)

    assert f'<a href="/project/{PROJECT}/claims" class="crumb-link">Claims</a>' in trail
    assert f'<span class="crumb-here" aria-current="page">{TOTAL_TEXT}</span>' in trail
    assert claim.citation.run_id not in trail


def test_the_nav_lights_the_claims_leaf_alone(claim, client):
    body = read_the_page(client, claim.id).text

    lit = re.findall(r'class="app-nav-item current"\s+href="([^"]+)"', body)
    assert lit == [f"/project/{PROJECT}/claims"]


def test_a_skip_has_no_sentence_so_its_trail_ends_on_the_metric(projects_root, client):
    run_id = run_the_fixture(projects_root)
    publish_the_outputs(run_id)
    skipped = claims_service.decline_output(PROJECT, run_id, "grant-total")

    trail = read_the_trail(read_the_page(client, skipped.id).text)

    assert f'<span class="crumb-here" aria-current="page">{TOTAL_SHAPE.label}</span>' in trail


def read_the_trail(body: str) -> str:
    return body.split('<nav class="crumbs"', 1)[1].split("</nav>", 1)[0]


# ── what an agent can do with a claim ─────


def test_submitting_through_the_tool_starts_the_review(claim, monkeypatch):
    from app.tools import claims as claim_tools
    started: list[str] = []
    monkeypatch.setattr(claim_tools.claim_review_run, "start_claim_review",
                        lambda project_id, claim_id, *, model: started.append(claim_id))

    written = claim_tools.submit_claim(
        PROJECT, claim.citation.run_id, "grant-total", "Grants came to 2,200 in total.")

    assert written.claim.status == "submitted"
    assert started == [written.claim.id]
    assert written.claim_url == f"/project/{PROJECT}/claims/{written.claim.id}"


def test_reading_a_review_before_one_runs_says_so(claim):
    from app.tools import claims as claim_tools

    read = claim_tools.read_claim_review(PROJECT, claim.id)

    assert read.review == "none" and read.challenges == []


def test_reading_a_stored_review_carries_every_challenge(claim):
    from app.tools import claims as claim_tools
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.high))

    read = claim_tools.read_claim_review(PROJECT, claim.id)

    assert read.review == "done"
    assert [one.severity for one in read.challenges] == [Severity.high]
    assert read.claim_text == claim.text
    assert read.claim_url == f"/project/{PROJECT}/claims/{claim.id}"


def test_cancelling_a_submission_leaves_nothing_standing(claim):
    from app.tools import claims as claim_tools

    withdrawn = claim_tools.cancel_claim_submission(PROJECT, claim.id)

    assert withdrawn.status == "declined"
