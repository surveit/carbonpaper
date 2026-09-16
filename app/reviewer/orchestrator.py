"""Building the one turn that sees every challenge at once, and the task it reads."""
from __future__ import annotations

import json

from app.core.agent.agent import Agent
from app.models.claim_review import EvidenceBundle, OrchestratorAnswer
from app.models.records.claim_review import DraftChallenge
from app.reviewer.evidence import render_evidence_bundle
from app.reviewer.orchestrator_prompt import ORCHESTRATOR_SYSTEM_PROMPT

# What the click asks for; the pool and what the reviewers raised follow it.
ORCHESTRATE_REQUEST = "merge what the reviewers found"

_CHALLENGES_HEADING = "----- CHALLENGES RAISED -----"


def build_orchestrator(
    bundle: EvidenceBundle, raised: list[DraftChallenge], *, model: str = "sonnet"
) -> Agent[OrchestratorAnswer]:
    return Agent(
        system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
        target_schema=OrchestratorAnswer,
        task=render_orchestrator_task(bundle, raised),
        model=model,
    )


def render_orchestrator_task(bundle: EvidenceBundle, raised: list[DraftChallenge]) -> str:
    return "\n\n".join([
        f"{ORCHESTRATE_REQUEST} — the sentence is quoted below with what the run holds, "
        "then every challenge raised against it. Return the merged list and the summary "
        "with submit_answer.",
        render_evidence_bundle(bundle),
        _render_challenges(raised),
    ])


def _render_challenges(raised: list[DraftChallenge]) -> str:
    # Unattributed: which reviewer raised one says nothing the kind does not already carry.
    dumped = json.dumps([one.model_dump(mode="json") for one in raised], indent=2)
    return f"{_CHALLENGES_HEADING}\n{dumped}"
