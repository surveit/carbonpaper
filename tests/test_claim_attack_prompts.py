"""Every attacker is told its place, and the orchestrator is told the guard is verbatim."""
from __future__ import annotations

import json
import re

from arch.test_no_banned_words import BANNED_WORDS
from pydantic import BaseModel

from app.compiler.claim_attack import attackers_prompt, orchestrator_prompt
from app.models.claim_review import (
    Attacker,
    ChallengeKind,
    ChallengesAnswer,
    ClaimReviewDraft,
    Cost,
    GroundingAnswer,
    MeaningAnswer,
    Moves,
)
from app.services.claim_review import (
    _read_whether_the_corpus_spells,
    find_grounding_issues,
    find_unbacked_challenges,
)

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

_DOCCS_SENTENCE = "A vast majority of guards accused of inmate abuse were never terminated."

_EXAMPLE_JSON: dict[str, tuple[str, type[BaseModel]]] = {
    "grounding": (attackers_prompt.GROUNDING_EXAMPLE_JSON, GroundingAnswer),
    "data_defects": (attackers_prompt.DATA_DEFECTS_EXAMPLE_JSON, ChallengesAnswer),
    "choices": (attackers_prompt.CHOICES_EXAMPLE_JSON, ChallengesAnswer),
    "omissions": (attackers_prompt.OMISSIONS_EXAMPLE_JSON, ChallengesAnswer),
    "coverage": (attackers_prompt.COVERAGE_EXAMPLE_JSON, ChallengesAnswer),
    "meaning": (attackers_prompt.MEANING_EXAMPLE_JSON, MeaningAnswer),
    "orchestrator": (orchestrator_prompt.ORCHESTRATOR_EXAMPLE_JSON, ClaimReviewDraft),
}

# The attacker cannot split rows or sum a column, so its example must not model one.
_CANNOT_COMPUTE_ITS_FIGURE = ["data_defects", "choices", "omissions"]

_EXAMPLE_POOL_LINES = {
    "grounding": attackers_prompt.GROUNDING_EXAMPLE_POOL_LINES,
    "data_defects": attackers_prompt.DATA_DEFECTS_EXAMPLE_POOL_LINES,
    "choices": attackers_prompt.CHOICES_EXAMPLE_POOL_LINES,
    "omissions": attackers_prompt.OMISSIONS_EXAMPLE_POOL_LINES,
    "coverage": attackers_prompt.COVERAGE_EXAMPLE_POOL_LINES,
    "meaning": attackers_prompt.MEANING_EXAMPLE_POOL_LINES,
    "orchestrator": orchestrator_prompt.ORCHESTRATOR_EXAMPLE_POOL_LINES,
}

# `text`, `why` and `summary` are prose; a figure only earns its place in these.
_FIELDS_CARRYING_A_COPIED_FIGURE = ("evidence", "backing", "how")


def _find_digit_runs(text: str) -> list[str]:
    # A digit inside an identifier (`lda_q1`, `figure5_counts`) is not a figure.
    return [run.strip(".,") for run in re.findall(r"(?<![A-Za-z0-9_])\d[\d,.]*", text)]


def _find_copied_figures(node: object) -> list[str]:
    if isinstance(node, dict):
        return [figure for key, value in node.items()
                for figure in (_find_digit_runs(value)
                               if isinstance(value, str) and key
                               in _FIELDS_CARRYING_A_COPIED_FIGURE
                               else _find_copied_figures(value))]
    if isinstance(node, list):
        return [figure for item in node for figure in _find_copied_figures(item)]
    return []


def test_each_attacker_is_told_who_reads_it_and_that_it_changes_nothing() -> None:
    for prompt in _ATTACKERS:
        assert "orchestrator" in prompt and "approve" in prompt
        assert "changes" in prompt and "submit_answer" in prompt


def test_the_orchestrator_is_told_to_copy_backings_verbatim() -> None:
    assert "verbatim" in _ORCHESTRATOR
    assert "published precision" in _ORCHESTRATOR


def test_the_orchestrator_is_told_the_pool_is_the_only_source_of_a_backing() -> None:
    assert "The pool is the only source" in _ORCHESTRATOR
    assert "not the attacker's `evidence` either" in _ORCHESTRATOR


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


def test_every_worked_example_validates_against_the_schema_it_is_written_for() -> None:
    for text, schema in _EXAMPLE_JSON.values():
        schema.model_validate(json.loads(text))


def test_every_worked_example_is_the_one_the_prompt_shows() -> None:
    for name, (text, _) in _EXAMPLE_JSON.items():
        prompt = (_ORCHESTRATOR if name == "orchestrator"
                  else getattr(attackers_prompt, f"{name.upper()}_SYSTEM_PROMPT"))
        assert text in prompt, f"{name}'s example is not in its prompt"


def test_the_grounding_example_spans_the_sentence_it_is_written_against() -> None:
    text, _ = _EXAMPLE_JSON["grounding"]
    answer = GroundingAnswer.model_validate(json.loads(text))
    assert find_grounding_issues(answer.phrases, _DOCCS_SENTENCE) == []
    assert [_DOCCS_SENTENCE[phrase.start:phrase.end] for phrase in answer.phrases] == [
        "A vast majority", "guards", "accused of inmate abuse", "never terminated"]


def test_the_orchestrator_example_backing_is_copied_off_a_pool_line() -> None:
    draft = ClaimReviewDraft.model_validate(
        json.loads(_EXAMPLE_JSON["orchestrator"][0]))
    assert find_unbacked_challenges(
        draft.challenges, _EXAMPLE_POOL_LINES["orchestrator"]) == []


def test_every_figure_an_example_writes_is_printed_on_the_pool_line_it_quotes() -> None:
    for name, (text, _) in _EXAMPLE_JSON.items():
        pool = _EXAMPLE_POOL_LINES[name]
        for figure in _find_copied_figures(json.loads(text)):
            assert _read_whether_the_corpus_spells(pool, figure), (
                f"{name} writes {figure!r}, which its pool lines do not print")


def test_every_example_shows_the_pool_lines_it_was_checked_against() -> None:
    for name, pool in _EXAMPLE_POOL_LINES.items():
        prompt = (_ORCHESTRATOR if name == "orchestrator"
                  else getattr(attackers_prompt, f"{name.upper()}_SYSTEM_PROMPT"))
        assert pool in prompt, f"{name}'s pool lines are not in its prompt"


def test_an_example_that_cannot_price_its_figure_takes_the_unpriced_route() -> None:
    for name in _CANNOT_COMPUTE_ITS_FIGURE:
        answer = ChallengesAnswer.model_validate(json.loads(_EXAMPLE_JSON[name][0]))
        assert [challenge.moves for challenge in answer.challenges] == [Moves.unpriced], name


def test_only_the_five_that_raise_challenges_are_shown_the_phrases_block() -> None:
    for prompt, _ in _KIND_RAISED:
        assert "----- PHRASES -----" in prompt
        assert "do not count" in prompt
    assert "----- PHRASES -----" not in attackers_prompt.GROUNDING_SYSTEM_PROMPT


def test_a_gap_is_not_capped_by_the_rule_that_caps_an_unpriced_finding() -> None:
    assert "A `gap` is the exception and is never capped" in _ORCHESTRATOR
    assert "comma or a full stop sitting right after" in _ORCHESTRATOR
