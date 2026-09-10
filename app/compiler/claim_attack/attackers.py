"""Building one of the six attackers over a claim's bundle, and the task it reads."""
from __future__ import annotations

import json

from app.compiler.claim_attack.attackers_prompt import (
    CHOICES_SYSTEM_PROMPT,
    COVERAGE_SYSTEM_PROMPT,
    DATA_DEFECTS_SYSTEM_PROMPT,
    GROUNDING_SYSTEM_PROMPT,
    MEANING_SYSTEM_PROMPT,
    OMISSIONS_SYSTEM_PROMPT,
)
from app.compiler.claim_attack.evidence import render_evidence_bundle
from app.core.agent.agent import Agent
from app.models.claim_review import (
    Attacker,
    ChallengesAnswer,
    EvidenceBundle,
    EvidenceRef,
    Grounding,
    GroundingAnswer,
    MeaningAnswer,
)

# What the journalist's click asks for; the claim and what the run holds follow it.
ATTACK_REQUEST = "attack this claim"

ATTACKERS: tuple[Attacker, ...] = (
    Attacker.grounding,
    Attacker.data_defects,
    Attacker.choices,
    Attacker.omissions,
    Attacker.coverage,
    Attacker.meaning,
)

_SYSTEM_PROMPTS: dict[Attacker, str] = {
    Attacker.grounding: GROUNDING_SYSTEM_PROMPT,
    Attacker.data_defects: DATA_DEFECTS_SYSTEM_PROMPT,
    Attacker.choices: CHOICES_SYSTEM_PROMPT,
    Attacker.omissions: OMISSIONS_SYSTEM_PROMPT,
    Attacker.coverage: COVERAGE_SYSTEM_PROMPT,
    Attacker.meaning: MEANING_SYSTEM_PROMPT,
}

_PHRASES_HEADING = "----- PHRASES -----"
_NOTHING_IN_THE_RUN = "nothing in the run"


def build_attacker(
    attacker: Attacker,
    bundle: EvidenceBundle,
    *,
    grounding: GroundingAnswer | None = None,
    model: str = "sonnet",
) -> Agent[GroundingAnswer] | Agent[ChallengesAnswer] | Agent[MeaningAnswer]:
    # Rendering refuses a mismatched pair, so building one refuses it too.
    task = render_attack_task(attacker, bundle, grounding)
    prompt = _SYSTEM_PROMPTS[attacker]
    if attacker is Attacker.grounding:
        return Agent(system_prompt=prompt, target_schema=GroundingAnswer,
                     task=task, model=model)
    if attacker is Attacker.meaning:
        return Agent(system_prompt=prompt, target_schema=MeaningAnswer, task=task, model=model)
    return Agent(system_prompt=prompt, target_schema=ChallengesAnswer, task=task, model=model)


def render_attack_task(
    attacker: Attacker, bundle: EvidenceBundle, grounding: GroundingAnswer | None = None
) -> str:
    """Every attacker is handed the same pool; what it reads that pool for is its prompt."""
    _refuse_a_mismatched_grounding(attacker, grounding)
    blocks = [
        f"{ATTACK_REQUEST} — the sentence is quoted below with what the run holds. "
        "Return your answer with submit_answer.",
        render_evidence_bundle(bundle),
    ]
    if grounding is not None:
        blocks.append(_render_phrases(bundle.claim_text, grounding))
    return "\n\n".join(blocks)


def _refuse_a_mismatched_grounding(
    attacker: Attacker, grounding: GroundingAnswer | None
) -> None:
    if attacker not in _SYSTEM_PROMPTS:
        raise ValueError(f"`{attacker.value}` is not one of the six attackers")
    if attacker is Attacker.grounding and grounding is not None:
        raise ValueError("the grounding attacker reads the claim first and alone")
    if attacker is not Attacker.grounding and grounding is None:
        raise ValueError(f"the `{attacker.value}` attacker needs the grounding attacker's phrases")


def _render_phrases(claim_text: str, grounding: GroundingAnswer) -> str:
    lines = [_render_phrase(index, claim_text, phrase)
             for index, phrase in enumerate(grounding.phrases)]
    return "\n".join([_PHRASES_HEADING, *lines])


def _render_phrase(index: int, claim_text: str, phrase: Grounding) -> str:
    return (f'[{index}] "{claim_text[phrase.start:phrase.end]}" → '
            f"{_render_evidence_ref(phrase.evidence)}")


def _render_evidence_ref(evidence: EvidenceRef | None) -> str:
    if evidence is None:
        return _NOTHING_IN_THE_RUN
    # json.dumps, not model_dump_json: the prompt's own example of a phrase line is spaced.
    return json.dumps(evidence.model_dump(mode="json"))
