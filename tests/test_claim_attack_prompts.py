"""Every attacker is told its place, and the orchestrator is told the guard is verbatim."""
from __future__ import annotations

import json
import re

from arch.test_no_banned_words import BANNED_WORDS
from pydantic import BaseModel

from app.compiler.claim_attack import attackers_prompt, orchestrator_prompt
from app.compiler.claim_attack.evidence import render_evidence_pool
from app.models.claim_review import (
    Attacker,
    BranchEvidenceItem,
    ChallengeKind,
    ChallengesAnswer,
    ClaimReviewDraft,
    CitedShape,
    Cost,
    EvidenceBundle,
    GroundingAnswer,
    InputColumnEvidenceItem,
    MeaningAnswer,
    Moves,
    StageEvidenceItem,
    find_grounding_issues,
)
from app.models.claims import StageOutputCellCitation
from app.services.claim_review import (
    _read_whether_the_corpus_spells,
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

_A_PHRASES_LINE = re.compile(r'\[(\d+)\] "([^"]+)" → (.+)')

_NUMBERLESS_BACKINGS = ["reads: none", "rows 0", "distinct 0",
                        "feeds the cited stage: false"]

# Priced off the moved VALUE. The rule is the printed population, so these must stay gone.
_THE_VALUE_BASED_RULE = [
    "you cannot compute it: then `moves` is `unpriced`",
    "What the share READS on the other column is on no line at all",
    "Price it only where the pool already prints both sides",
]

# One of everything, all of it empty: the lines a challenge with no number is backed on.
_AN_EMPTY_POOL = EvidenceBundle(
    project_id="p", run_id="r", claim_id="c", claim_text=_DOCCS_SENTENCE,
    claim_context={}, run_read_everything=False,
    cited=StageOutputCellCitation(run_id="r", stage_id="ia_job_status", row_ordinal=0,
                                  column="share", value=0),
    shape=CitedShape(label="", universe="", importance="", qualifiers=[],
                     context_columns=[]),
    outputs=[],
    stages=[StageEvidenceItem(stage_id="cases", type="input_data", description="",
                              input_ids=[], code="", feeds_the_cited_stage=False)],
    branches=[BranchEvidenceItem(branch_id="cases|classify/0:if", stage_id="cases",
                                 reason="code", role="keeps", label="", source_code="",
                                 rows_count=0)],
    input_columns=[InputColumnEvidenceItem(stage_id="cases", column="s_GUID", kind="empty",
                                           row_count=0, filled_count=0, null_count=0,
                                           blank_count=0, distinct_count=0, top=[])],
    terms="", methodology=None)


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


def test_the_phrases_block_lands_its_lines_where_the_grounding_example_lands_them() -> None:
    answer = GroundingAnswer.model_validate(
        json.loads(attackers_prompt.GROUNDING_EXAMPLE_JSON))
    for prompt, _ in _KIND_RAISED:
        shown = _A_PHRASES_LINE.findall(prompt)
        assert shown, "the phrases block illustrates no line"
        for index, phrase, ref in shown:
            landed = answer.phrases[int(index)]
            assert _DOCCS_SENTENCE[landed.start:landed.end] == phrase
            assert landed.evidence is not None, f"[{index}] lands on nothing"
            assert json.loads(ref) == landed.evidence.model_dump()


def test_a_challenge_with_no_number_is_backed_on_a_line_the_pool_can_print() -> None:
    pool = render_evidence_pool(_AN_EMPTY_POOL)
    for backing in _NUMBERLESS_BACKINGS:
        assert f"`{backing}`" in _ORCHESTRATOR, f"{backing} is not offered as a backing"
        assert _read_whether_the_corpus_spells(pool, backing), (
            f"the renderer prints no line spelling {backing!r}")


def test_the_pricing_rule_draws_the_line_between_moves_and_unpriced() -> None:
    for prompt, _ in _KIND_RAISED:
        assert "The line between `moves` and `unpriced` is drawn once, here." in prompt
        assert "population that moves" in prompt
        assert "prints neither the population nor the value" in prompt


def test_no_prompt_teaches_the_value_based_rule_the_population_rule_replaced() -> None:
    for prompt in [*_ATTACKERS, _ORCHESTRATOR]:
        for sentence in _THE_VALUE_BASED_RULE:
            assert sentence not in prompt, f"{sentence!r} is back"


def test_the_cap_turns_on_the_printed_value_and_not_on_moves_against_unpriced() -> None:
    cap = _ORCHESTRATOR[_ORCHESTRATOR.index("THE CAP ON WHAT IT MOVES"):]
    cap = cap[:cap.index("\n")]
    assert "`moves` reads `moves` or `unpriced`" in cap
    assert "never the weight" in cap
    assert "A `gap` is the exception and is never capped" in cap


def test_the_worked_example_weighs_the_bound_the_way_the_cap_does() -> None:
    assert "which is weight 2 on its own" not in _ORCHESTRATOR
    example = _ORCHESTRATOR[_ORCHESTRATOR.index("WORKED EXAMPLE."):]
    assert "the cap holds that route to 1" in example
    assert "What carries it to 3 is the footing" in example


def test_a_gap_is_not_capped_by_the_rule_that_caps_an_unpriced_finding() -> None:
    assert "A `gap` is the exception and is never capped" in _ORCHESTRATOR
    assert "comma or a full stop sitting right after" in _ORCHESTRATOR
