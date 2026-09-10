"""The claim page: the sentence with its ground, and what state its attack is in."""
from __future__ import annotations

from typing import Any

import pytest

from app.core.agent.store import SessionStore
from app.models.claim_review import (
    SEVERITY_WORDS,
    Attacker,
    Challenge,
    ChallengeKind,
    Cost,
    Grounding,
    Moves,
    OutputEvidence,
    Rewrite,
)
from app.models.claims import RowsRectangle, StageOutputTableCitation
from app.models.records.claim_review import ClaimReview
from app.models.records.claims import Claim
from app.models.records.workflow_output import WorkflowOutput
from app.services import claims as claims_service
from app.services.claim_review import PARENT_ROLE, store_claim_review
from app.services.generation import GENERATION_FAILURE_PREFIX
from app.web.claim_review_view import build_claim_review_page
from claim_review_fixture import (
    PROJECT,
    TOTAL_TEXT,
    claim_the_total,
    publish_the_outputs,
    run_the_fixture,
)

_FIGURE = "2,200"
_AT = TOTAL_TEXT.index(_FIGURE)
_WHOLE = "in total"
# The backings, spelled as the evidence pool spells them; nothing else backs a challenge.
_CORPUS = "grant-total 2200 · grant-count 5 grants"
_SUMMARY = "It stands as a total of the rows the run read."


@pytest.fixture
def claim(projects_root) -> Claim:
    return claim_the_total(run_the_fixture(projects_root))


def store_a_review(claim_id: str) -> ClaimReview:
    return store_claim_review(
        PROJECT, claim_id,
        grounding=[Grounding(
            start=_AT, end=_AT + len(_FIGURE), evidence=OutputEvidence(slug="grant-total"),
            how="the figure is read straight off the cited output")],
        challenges=[
            Challenge(
                attacker=Attacker.data_defects, kind=ChallengeKind.data, grounding_index=0,
                text="The total counts rows, not grants.",
                evidence="the amount column is blank on two rows", backing="2200",
                severity=3, moves=Moves.moves, cost=Cost.free, raised_by="the amount column"),
            Challenge(
                attacker=Attacker.coverage, kind=ChallengeKind.coverage, grounding_index=0,
                text="Every grant the file records is in the count.",
                evidence="no filter drops a row before the total", backing="5 grants",
                severity=0, moves=Moves.none, cost=Cost.settled),
        ],
        rewrites=[Rewrite(text="Five grants were recorded.", why="counts what was counted")],
        summary=_SUMMARY, session_ids=["first", "last"], corpus=_CORPUS,
    )


def _ground_the(phrase: str) -> Grounding:
    at = TOTAL_TEXT.index(phrase)
    return Grounding(start=at, end=at + len(phrase), evidence=OutputEvidence(slug="grant-total"),
                     how=f"{phrase} is read off the cited output")


def _raise_on(index: int, *, severity: int, backing: str) -> Challenge:
    return Challenge(
        attacker=Attacker.choices, kind=ChallengeKind.choice, grounding_index=index,
        text="A cut was taken here.", evidence="the stage code takes one reading",
        backing=backing, severity=severity, moves=Moves.moves, cost=Cost.free)


def open_a_parent_session(claim_id: str) -> tuple[SessionStore, str]:
    store = SessionStore()
    session_id = store.create(
        title=f"Attack · claim {claim_id}",
        context={"project_id": PROJECT, "claim_id": claim_id, "hidden": True,
                 "role": PARENT_ROLE})
    return store, session_id


def fail_the_turn(store: SessionStore, session_id: str, error: str) -> None:
    store.append_messages(session_id, [{
        "role": "assistant",
        "parts": [{"type": "text", "text": f"{GENERATION_FAILURE_PREFIX}{error}"}],
    }])


# ── a claim nobody has attacked ─────


def test_a_claim_with_no_review_reads_as_one_plain_sentence(claim):
    page = build_claim_review_page(PROJECT, claim.id)

    assert page.attack == "none"
    assert page.attack_session_id is None and page.attack_error is None
    assert [token.text for token in page.tokens] == [TOTAL_TEXT]
    assert page.tokens[0].grounding_index is None
    assert page.tokens[0].severity is None
    assert page.ground == []
    assert page.open_challenges == [] and page.quiet_challenges == []
    assert page.summary == "" and page.session_ids == []


def test_the_page_carries_what_the_run_read_and_what_blocks_it(claim):
    page = build_claim_review_page(PROJECT, claim.id)

    assert page.run_id == claim.citation.run_id
    assert page.run_href == f"/project/{PROJECT}/runs/{claim.citation.run_id}"
    assert page.blocked == ""
    assert page.run_read_everything is True
    assert page.value == "2200"
    assert page.status == "submitted"
    assert page.shape_label == "What the grants came to, in whole units"
    assert page.universe == "closed"
    assert [output.slug for output in page.outputs] == ["grant-count", "grant-total"]
    assert [output.cited for output in page.outputs] == [False, True]


# ── a claim the attackers have read ─────


def test_a_stored_review_underlines_the_phrase_it_grounds(claim):
    store_a_review(claim.id)

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.attack == "done"
    assert [token.text for token in page.tokens] == ["Grants came to ", _FIGURE, " in total."]
    [figure] = [token for token in page.tokens if token.text == _FIGURE]
    assert figure.grounding_index == 0
    assert figure.severity == 3
    assert [token.severity for token in page.tokens] == [None, 3, None]


def test_a_stored_review_bands_its_challenges_by_severity(claim):
    store_a_review(claim.id)

    page = build_claim_review_page(PROJECT, claim.id)

    [opened] = page.open_challenges
    assert opened.severity == 3
    assert opened.severity_words == SEVERITY_WORDS[3]
    assert opened.phrase == _FIGURE
    assert opened.attacker == "data_defects"
    assert opened.kind_words == "data"
    assert opened.moves_words == "moves the figure"
    assert opened.cost_words == "free rerun"
    assert opened.raised_by == "the amount column"
    [quiet] = page.quiet_challenges
    assert quiet.severity == 0
    assert quiet.severity_words == SEVERITY_WORDS[0]
    assert quiet.backing == "5 grants"


def test_open_challenges_run_worst_first_then_in_reading_order(claim):
    store_claim_review(
        PROJECT, claim.id,
        grounding=[_ground_the(_FIGURE), _ground_the(_WHOLE)],
        challenges=[
            _raise_on(0, severity=2, backing="2200"),
            _raise_on(1, severity=2, backing="5 grants"),
            _raise_on(1, severity=3, backing="grant-count"),
        ],
        rewrites=[], summary=_SUMMARY, session_ids=[], corpus=_CORPUS)

    page = build_claim_review_page(PROJECT, claim.id)

    assert [(one.severity, one.phrase) for one in page.open_challenges] == [
        (3, _WHOLE), (2, _FIGURE), (2, _WHOLE)]
    assert [token.severity for token in page.tokens] == [None, 2, None, 3, None]


def test_a_stored_review_carries_its_ground_rewrites_and_summary(claim):
    store_a_review(claim.id)

    page = build_claim_review_page(PROJECT, claim.id)

    [ground] = page.ground
    assert ground.phrase == _FIGURE
    assert ground.lands_on == "output grant-total"
    assert ground.how == "the figure is read straight off the cited output"
    [rewrite] = page.rewrites
    assert rewrite.text == "Five grants were recorded."
    assert rewrite.why == "counts what was counted"
    assert page.summary == _SUMMARY
    assert page.session_ids == ["first", "last"]


def test_the_page_counts_every_attacker_including_the_silent_ones(claim):
    store_a_review(claim.id)

    page = build_claim_review_page(PROJECT, claim.id)

    counted = {one.name: one.count for one in page.attackers}
    assert counted == {"Grounding": 0, "Data defects": 1, "Choices made": 0,
                       "Decisions never made": 0, "Coverage": 1, "Meaning": 0}
    assert [one.reads for one in page.attackers if one.name == "Coverage"] == [
        "filters' dropped rows and the shape's open/closed word"]


# ── an attack that is still running, or that failed ─────


def test_an_attack_with_a_turn_in_flight_reads_as_running(claim):
    store, session_id = open_a_parent_session(claim.id)
    store.set_active_turn(session_id, "attack")

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.attack == "running"
    assert page.attack_session_id == session_id
    assert page.attack_error is None


def test_an_attack_that_failed_says_what_it_said(claim):
    store, session_id = open_a_parent_session(claim.id)
    fail_the_turn(store, session_id, "the model returned no answer")

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.attack == "failed"
    assert page.attack_session_id == session_id
    assert page.attack_error == f"{GENERATION_FAILURE_PREFIX}the model returned no answer"


def test_a_session_on_another_claim_says_nothing_about_this_one(claim):
    store, session_id = open_a_parent_session("some-other-claim")
    store.set_active_turn(session_id, "attack")

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.attack == "none"
    assert page.attack_session_id is None


# ── a claim with no sentence to attack ─────


def test_a_table_claim_is_refused_rather_than_attacked(projects_root):
    run_id = run_the_fixture(projects_root)
    shape = publish_the_outputs(run_id)
    _publish_a_table(run_id, shape.id)
    claim = claims_service.submit_claim(
        PROJECT, run_id, "grant-rows", {}, "The grants are these five.")

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.attack == "refused"
    assert page.value == "5 rows"
    assert [token.text for token in page.tokens] == ["The grants are these five."]
    assert page.ground == []


def _publish_a_table(run_id: str, shape_id: str) -> Any:
    WorkflowOutput(
        slug="grant-rows", label="The grants themselves", primary=False, shape_id=shape_id,
        citation=StageOutputTableCitation(
            run_id=run_id, stage_id="grant_totals",
            rectangle=RowsRectangle(row_start=0, row_end=5, columns=["amount"])),
    ).save()
