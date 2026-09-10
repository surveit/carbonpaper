"""Building the one turn that sees all six answers at once, and the task it reads."""
from __future__ import annotations

from pydantic import BaseModel

from app.compiler.claim_attack.evidence import render_evidence_bundle
from app.compiler.claim_attack.orchestrator_prompt import ORCHESTRATOR_SYSTEM_PROMPT
from app.core.agent.agent import Agent
from app.models.claim_review import Attacker, AttackerAnswers, ClaimReviewDraft, EvidenceBundle

# What the journalist's click asks for; the pool and the six answers follow it.
ORCHESTRATE_REQUEST = "merge what the attackers found"

_ANSWERS_HEADING = "----- ANSWERS -----"


def build_orchestrator(
    bundle: EvidenceBundle, answers: AttackerAnswers, *, model: str = "sonnet"
) -> Agent[ClaimReviewDraft]:
    return Agent(
        system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
        target_schema=ClaimReviewDraft,
        task=render_orchestrator_task(bundle, answers),
        model=model,
    )


def render_orchestrator_task(bundle: EvidenceBundle, answers: AttackerAnswers) -> str:
    return "\n\n".join([
        f"{ORCHESTRATE_REQUEST} — the sentence is quoted below with what the run holds, "
        "then what each of the six returned. Return the merged list and the summary with "
        "submit_answer.",
        render_evidence_bundle(bundle),
        _render_answers(answers),
    ])


def _render_answers(answers: AttackerAnswers) -> str:
    return "\n\n".join([_ANSWERS_HEADING, *(
        f"## {attacker.value}\n{answer.model_dump_json(indent=2)}"
        for attacker, answer in _list_the_answers(answers)
    )])


def _list_the_answers(answers: AttackerAnswers) -> list[tuple[Attacker, BaseModel]]:
    return [
        (Attacker.grounding, answers.grounding),
        (Attacker.data_defects, answers.data_defects),
        (Attacker.choices, answers.choices),
        (Attacker.omissions, answers.omissions),
        (Attacker.coverage, answers.coverage),
        (Attacker.meaning, answers.meaning),
    ]
