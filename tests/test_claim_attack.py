"""Six attackers built over one bundle, and an orchestrator built over their six answers."""
from __future__ import annotations

import pytest

from app.compiler.claim_attack.attackers import (
    ATTACK_REQUEST,
    ATTACKERS,
    build_attacker,
    render_attack_task,
)
from app.compiler.claim_attack.orchestrator import (
    build_orchestrator,
    render_orchestrator_task,
)
from app.models.claim_review import (
    Attacker,
    AttackerAnswers,
    ChallengeKind,
    ChallengesAnswer,
    Cost,
    Grounding,
    GroundingAnswer,
    InputColumnEvidence,
    MeaningAnswer,
    Moves,
    OutputEvidence,
    RaisedChallenge,
    Rewrite,
)
from app.services import claim_review
from claim_review_fixture import PROJECT, TOTAL_TEXT, claim_the_total, run_the_fixture

_SUBMIT_ONLY = ["mcp__tools__submit_answer"]
_LATER = [attacker for attacker in ATTACKERS if attacker is not Attacker.grounding]


@pytest.fixture
def bundle(projects_root):
    claim = claim_the_total(run_the_fixture(projects_root))
    return claim_review.build_evidence_bundle(PROJECT, claim.id)


def make_grounding() -> GroundingAnswer:
    total = TOTAL_TEXT.index("in total")
    return GroundingAnswer(phrases=[
        Grounding(start=0, end=len("Grants"), evidence=OutputEvidence(slug="grant-total"),
                  how="the cited figure counts the grant rows"),
        Grounding(start=total, end=total + len("in total"), evidence=None,
                  how="nothing in the run says the file is the whole of it"),
    ])


def make_challenge(evidence: str) -> RaisedChallenge:
    return RaisedChallenge(
        kind=ChallengeKind.data, grounding_index=0, text="The figure counts rows, not grants.",
        evidence=evidence, moves=Moves.moves, cost=Cost.free,
    )


def make_answers() -> AttackerAnswers:
    return AttackerAnswers(
        grounding=make_grounding(),
        data_defects=ChallengesAnswer(challenges=[make_challenge("the amount column is blank")]),
        choices=ChallengesAnswer(challenges=[make_challenge("the filter drops the zeroes")]),
        omissions=ChallengesAnswer(challenges=[make_challenge("the west file is unread")]),
        coverage=ChallengesAnswer(challenges=[make_challenge("one arm took no rows")]),
        meaning=MeaningAnswer(
            challenges=[make_challenge("total reads as money, not a count")],
            rewrites=[Rewrite(text="Five grants were recorded.", why="counts what was counted")],
        ),
    )


# ── what an attacker is handed ─────


def test_every_attacker_holds_no_tool_but_submit_answer(bundle) -> None:
    for attacker in ATTACKERS:
        given = None if attacker is Attacker.grounding else make_grounding()

        engine = build_attacker(attacker, bundle, grounding=given).build_engine()

        assert engine._allowed_tools == _SUBMIT_ONLY, attacker
        assert engine._builtin_tools == [], attacker


def test_the_task_opens_with_the_request_and_carries_the_sentence_and_the_run(bundle) -> None:
    task = render_attack_task(Attacker.grounding, bundle)

    assert task.startswith(ATTACK_REQUEST)
    assert TOTAL_TEXT in task
    assert "----- BRANCHES -----" in task


def test_a_later_attacker_reads_the_phrases_the_grounding_attacker_landed(bundle) -> None:
    task = render_attack_task(Attacker.data_defects, bundle, make_grounding())

    assert "----- PHRASES -----" in task
    assert '[0] "Grants" → {"kind": "output", "slug": "grant-total"}' in task
    assert '[1] "in total" → nothing in the run' in task


def test_a_ref_spells_a_column_the_way_the_pool_spells_it(bundle) -> None:
    accented = GroundingAnswer(phrases=[
        Grounding(start=0, end=len("Grants"),
                  evidence=InputColumnEvidence(stage_id="cases", column="café"),
                  how="the column the figure counts"),
    ])

    task = render_attack_task(Attacker.data_defects, bundle, accented)

    assert '[0] "Grants" → {"kind": "input_column", "stage_id": "cases", "column": "café"}' in task
    assert r"caf\u00e9" not in task


def test_a_phrase_reaching_past_the_sentence_is_refused(bundle) -> None:
    past = GroundingAnswer(phrases=[
        Grounding(start=0, end=len(bundle.claim_text) + 5, evidence=None,
                  how="more of the sentence than was written"),
    ])

    with pytest.raises(ValueError, match="past the end"):
        render_attack_task(Attacker.data_defects, bundle, past)


def test_two_phrases_landing_on_the_same_words_are_refused(bundle) -> None:
    crossing = GroundingAnswer(phrases=[
        Grounding(start=0, end=10, evidence=None, how="the first"),
        Grounding(start=5, end=12, evidence=None, how="the second, over the first"),
    ])

    with pytest.raises(ValueError, match="overlaps"):
        render_attack_task(Attacker.data_defects, bundle, crossing)


def test_the_grounding_attacker_reads_the_claim_before_any_phrase_exists(bundle) -> None:
    task = render_attack_task(Attacker.grounding, bundle)

    assert "----- PHRASES -----" not in task


def test_a_later_attacker_without_the_phrases_is_refused(bundle) -> None:
    for attacker in _LATER:
        with pytest.raises(ValueError):
            build_attacker(attacker, bundle)


def test_the_grounding_attacker_handed_phrases_is_refused(bundle) -> None:
    with pytest.raises(ValueError):
        build_attacker(Attacker.grounding, bundle, grounding=make_grounding())


def test_the_orchestrator_is_not_one_of_the_six(bundle) -> None:
    with pytest.raises(ValueError):
        build_attacker(Attacker.orchestrator, bundle, grounding=make_grounding())


def test_each_attacker_answers_in_its_own_shape(bundle) -> None:
    phrases = make_grounding()

    assert build_attacker(Attacker.grounding, bundle)._target_schema is GroundingAnswer
    assert build_attacker(
        Attacker.meaning, bundle, grounding=phrases)._target_schema is MeaningAnswer
    assert build_attacker(
        Attacker.choices, bundle, grounding=phrases)._target_schema is ChallengesAnswer


# ── what the orchestrator is handed ─────


def test_the_orchestrator_holds_no_tool_but_submit_answer(bundle) -> None:
    engine = build_orchestrator(bundle, make_answers()).build_engine()

    assert engine._allowed_tools == _SUBMIT_ONLY
    assert engine._builtin_tools == []


def test_the_orchestrator_reads_the_pool_and_every_attackers_evidence(bundle) -> None:
    answers = make_answers()

    task = render_orchestrator_task(bundle, answers)

    assert TOTAL_TEXT in task and "----- BRANCHES -----" in task
    assert "----- ANSWERS -----" in task
    for evidence in answers.list_evidence():
        assert evidence in task


def test_the_orchestrator_reads_the_answers_under_their_attackers_grounding_first(
    bundle,
) -> None:
    task = render_orchestrator_task(bundle, make_answers())

    headings = [line for line in task.splitlines() if line.startswith("## ")]
    assert headings == [f"## {attacker.value}" for attacker in ATTACKERS]
    # The grounding answer arrives as its own JSON here, never as the attackers' phrase block.
    assert '"rewrites"' in task and "----- PHRASES -----" not in task


def test_list_evidence_reads_the_five_challenge_answers_in_attacker_order() -> None:
    assert make_answers().list_evidence() == [
        "the amount column is blank", "the filter drops the zeroes", "the west file is unread",
        "one arm took no rows", "total reads as money, not a count",
    ]
