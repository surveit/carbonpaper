"""Every reviewer is told its place, its one kind, and shown an example of the answer."""
from __future__ import annotations

import json
import re

from arch.test_no_banned_words import BANNED_WORDS

from app.models.claim_review import ChallengesAnswer
from app.models.records.claim_review import SEVERITY_WORDS, ChallengeKind
from app.reviewer import reviewers_prompt as prompts

_BY_REVIEWER: dict[str, tuple[str, ChallengeKind, str, str]] = {
    "data_defects": (prompts.DATA_DEFECTS_SYSTEM_PROMPT, ChallengeKind.data,
                     prompts.DATA_DEFECTS_EXAMPLE_JSON, prompts.DATA_DEFECTS_EXAMPLE_POOL_LINES),
    "choices": (prompts.CHOICES_SYSTEM_PROMPT, ChallengeKind.choice,
                prompts.CHOICES_EXAMPLE_JSON, prompts.CHOICES_EXAMPLE_POOL_LINES),
    "omissions": (prompts.OMISSIONS_SYSTEM_PROMPT, ChallengeKind.omission,
                  prompts.OMISSIONS_EXAMPLE_JSON, prompts.OMISSIONS_EXAMPLE_POOL_LINES),
    "coverage": (prompts.COVERAGE_SYSTEM_PROMPT, ChallengeKind.coverage,
                 prompts.COVERAGE_EXAMPLE_JSON, prompts.COVERAGE_EXAMPLE_POOL_LINES),
    "meaning": (prompts.MEANING_SYSTEM_PROMPT, ChallengeKind.meaning,
                prompts.MEANING_EXAMPLE_JSON, prompts.MEANING_EXAMPLE_POOL_LINES),
}

_EVERY_PROMPT = [prompt for prompt, _kind, _json, _pool in _BY_REVIEWER.values()]

# A digit inside an identifier (`lda_q1`, `figure5_counts`) is not a figure.
_A_FIGURE = re.compile(r"(?<![A-Za-z0-9_])\d[\d,.]*")

# `text` is prose. A figure earns its place in the sentence that says what the run holds.
_FIELD_CARRYING_A_COPIED_FIGURE = "justification"

# The vocabulary the design retired; a prompt that says one of these is describing
# a field no answer carries.
_RETIRED = ("`moves`", "`cost`", "unpriced", "grounding_index", "evidence_refs",
            "raised_by", "orchestrator", "attacker", "journalist", "backing")


def test_every_reviewer_is_told_who_reads_it_and_that_it_changes_nothing() -> None:
    for prompt in _EVERY_PROMPT:
        assert "deciding whether to approve it" in prompt
        assert "nothing you write changes the claim" in prompt


def test_no_prompt_names_a_field_no_answer_carries() -> None:
    said = {word for prompt in _EVERY_PROMPT for word in _RETIRED if word in prompt.lower()}

    assert said == set()


def test_every_prompt_is_written_and_uses_no_banned_word() -> None:
    for prompt in _EVERY_PROMPT:
        assert len(prompt) > 500
        assert not [word for word in BANNED_WORDS if word in prompt.lower()]


def test_each_reviewer_names_the_one_kind_it_raises() -> None:
    for name, (prompt, kind, _json, _pool) in _BY_REVIEWER.items():
        assert f"`{kind.value}`" in prompt, name


def test_every_reviewer_carries_the_whole_severity_rubric() -> None:
    for name, (prompt, _kind, _json, _pool) in _BY_REVIEWER.items():
        for word in SEVERITY_WORDS.values():
            assert word in prompt, f"{name} is missing {word!r}"


def test_every_worked_example_validates_against_the_answer_schema() -> None:
    for name, (_prompt, _kind, example, _pool) in _BY_REVIEWER.items():
        answer = ChallengesAnswer.model_validate_json(example)

        assert answer.challenges, f"{name}'s example raises nothing"


def test_every_worked_example_raises_the_kind_its_reviewer_raises() -> None:
    for name, (_prompt, kind, example, _pool) in _BY_REVIEWER.items():
        answer = ChallengesAnswer.model_validate_json(example)

        assert [one.kind for one in answer.challenges] == [kind] * len(answer.challenges), name


def test_every_worked_example_cites_something_unless_it_is_a_gap() -> None:
    for name, (_prompt, _kind, example, _pool) in _BY_REVIEWER.items():
        for one in ChallengesAnswer.model_validate_json(example).challenges:
            assert one.citations or one.kind == ChallengeKind.gap, name


def test_every_prompt_shows_the_example_and_the_pool_lines_it_was_read_off() -> None:
    for name, (prompt, _kind, example, pool) in _BY_REVIEWER.items():
        assert example in prompt, f"{name} does not show its example"
        assert pool in prompt, f"{name} does not show the lines it was read off"


def test_every_figure_an_example_writes_is_printed_on_a_pool_line_it_shows() -> None:
    for name, (_prompt, _kind, example, pool) in _BY_REVIEWER.items():
        printed = set([run.strip(".,") for run in _A_FIGURE.findall(pool)])
        for written in _find_copied_figures(json.loads(example)):
            assert written in printed, f"{name} writes {written!r}, which its pool lines do not"


def _find_copied_figures(node: object) -> list[str]:
    if isinstance(node, dict):
        return [figure.strip(".,") for key, value in node.items()
                for figure in (_A_FIGURE.findall(value)
                               if isinstance(value, str) and key == _FIELD_CARRYING_A_COPIED_FIGURE
                               else _find_copied_figures(value))]
    if isinstance(node, list):
        return [figure for item in node for figure in _find_copied_figures(item)]
    return []
