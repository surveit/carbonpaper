"""Every attacker is told its place, and the orchestrator is told the guard is verbatim."""
from __future__ import annotations

from arch.test_no_banned_words import BANNED_WORDS

from app.compiler.claim_attack import attackers_prompt, orchestrator_prompt
from app.models.claim_review import Attacker, ChallengeKind, Cost, Moves

_ORCHESTRATOR = orchestrator_prompt.ORCHESTRATOR_SYSTEM_PROMPT

_ATTACKERS = [attackers_prompt.GROUNDING_SYSTEM_PROMPT, attackers_prompt.DATA_DEFECTS_SYSTEM_PROMPT,
              attackers_prompt.CHOICES_SYSTEM_PROMPT, attackers_prompt.OMISSIONS_SYSTEM_PROMPT,
              attackers_prompt.COVERAGE_SYSTEM_PROMPT, attackers_prompt.MEANING_SYSTEM_PROMPT]

# Grounding is absent: it returns phrases, and a null one becomes the orchestrator's `gap`.
_KIND_RAISED = [
    (attackers_prompt.DATA_DEFECTS_SYSTEM_PROMPT, ChallengeKind.data),
    (attackers_prompt.CHOICES_SYSTEM_PROMPT, ChallengeKind.choice),
    (attackers_prompt.OMISSIONS_SYSTEM_PROMPT, ChallengeKind.omission),
    (attackers_prompt.COVERAGE_SYSTEM_PROMPT, ChallengeKind.coverage),
    (attackers_prompt.MEANING_SYSTEM_PROMPT, ChallengeKind.semantic),
]


def test_each_attacker_is_told_who_reads_it_and_that_it_changes_nothing() -> None:
    for prompt in _ATTACKERS:
        assert "orchestrator" in prompt and "approve" in prompt
        assert "changes" in prompt and "submit_answer" in prompt


def test_the_orchestrator_is_told_to_copy_backings_verbatim() -> None:
    assert "verbatim" in _ORCHESTRATOR
    assert "published precision" in _ORCHESTRATOR


def test_every_prompt_is_written_and_uses_no_banned_word() -> None:
    for prompt in [*_ATTACKERS, _ORCHESTRATOR]:
        assert prompt.strip()
        assert not [word for word in BANNED_WORDS if word in prompt.lower()]


def test_each_attacker_names_the_one_kind_it_raises() -> None:
    for prompt, kind in _KIND_RAISED:
        assert f"`{kind.value}`" in prompt


def test_each_challenge_raising_attacker_spells_every_moves_and_cost_value() -> None:
    for prompt, _ in _KIND_RAISED:
        for value in [*Moves, *Cost]:
            assert f"`{value.value}`" in prompt, f"{value.value} is unnamed"


def test_the_orchestrator_spells_every_value_it_must_choose_among() -> None:
    for value in [*Attacker, *ChallengeKind, *Moves, *Cost]:
        assert f"`{value.value}`" in _ORCHESTRATOR, f"{value.value} is unnamed"


def test_the_orchestrator_carries_the_four_severity_rows_and_keeps_the_quiet_ones() -> None:
    for row in ["could not stand as written", "turns it into a bound",
                "worth a footnote", "does not move it"]:
        assert row in _ORCHESTRATOR
    assert "submit_answer" in _ORCHESTRATOR
