"""The review's models: spans, severities and evidence refs are shaped before any model runs."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.claim_review import (
    SEVERITY_WORDS,
    Attacker,
    Challenge,
    ChallengeKind,
    Cost,
    Grounding,
    MeaningAnswer,
    Moves,
    OutputEvidence,
    RaisedChallenge,
    Rewrite,
)


def _challenge(**overrides) -> Challenge:
    fields = dict(attacker=Attacker.coverage, kind=ChallengeKind.coverage, grounding_index=0,
                  text="Blank outcomes count as unknown.", evidence="28% of records are blank.",
                  backing="28% of records", severity=3, moves=Moves.moves, cost=Cost.free)
    return Challenge(**{**fields, **overrides})


def test_a_grounding_lands_on_one_piece_of_the_run_or_on_nothing():
    landed = Grounding(start=0, end=14, evidence=OutputEvidence(slug="ai-spend"), how="the figure")
    nowhere = Grounding(start=15, end=20, evidence=None, how="no figure, column, term or stage")
    assert landed.evidence is not None and landed.evidence.kind == "output"
    assert nowhere.evidence is None


def test_an_evidence_ref_is_told_apart_by_its_kind():
    parsed = Grounding.model_validate(
        {"start": 0, "end": 3, "how": "x", "evidence": {"kind": "term", "name": "filing"}})
    assert parsed.evidence is not None and parsed.evidence.kind == "term"
    with pytest.raises(ValidationError):
        Grounding.model_validate({"start": 0, "end": 3, "how": "x", "evidence": {"kind": "rumour"}})


def test_severity_runs_from_zero_to_three_and_each_has_a_word():
    assert [_challenge(severity=s).severity for s in range(4)] == [0, 1, 2, 3]
    assert set(SEVERITY_WORDS) == {0, 1, 2, 3}
    with pytest.raises(ValidationError):
        _challenge(severity=4)


def test_a_raised_challenge_carries_no_severity_and_no_backing():
    raised = RaisedChallenge(kind=ChallengeKind.data, text="t", evidence="e",
                             moves=Moves.none, cost=Cost.free)
    assert not hasattr(raised, "severity") and not hasattr(raised, "backing")


def test_the_meaning_attacker_may_propose_at_most_two_rewrites():
    three = [Rewrite(text=f"reading {i}", why="w") for i in range(3)]
    with pytest.raises(ValidationError):
        MeaningAnswer(challenges=[], rewrites=three)
    assert len(MeaningAnswer(challenges=[], rewrites=three[:2]).rewrites) == 2
