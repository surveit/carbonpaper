"""One eval case: the claim a row names, reviewed, its challenges handed back as row data."""
from __future__ import annotations

import json
from typing import TypeAlias

from app.core.agent.store import open_session_store
from app.core.ids import ID
from app.core.json_types import JsonDict
from app.core.llm_sdk import run_sync
from app.models.records.claim_review import ChallengeKind, DraftChallenge, Severity
from app.reviewer.run import review_claim
from app.services.claim_review import build_evidence_bundle
from app.services.project import project_meta

# One eval case as the workflow's own input data spells it; nothing has checked its keys.
EvalCaseRow: TypeAlias = JsonDict
# What the row-mapped stage writes: JSON-safe scalars, so arrow types every column.
ReviewedClaimRow: TypeAlias = JsonDict

PROJECT_COLUMN = "project_id"
CLAIM_COLUMN = "claim_id"
MODEL_COLUMN = "model"


def review_the_claim_a_row_names(row: EvalCaseRow) -> ReviewedClaimRow:
    project_id = _read_required_id(row, PROJECT_COLUMN)
    claim_id = _read_required_id(row, CLAIM_COLUMN)
    bundle = build_evidence_bundle(project_id, claim_id)
    model = _read_model(row, project_id)
    session_id = _open_its_own_session_id(project_id, claim_id)
    result = run_sync(review_claim(bundle, model=model, session_id=session_id))
    return _render_row(result.challenges, session_id)


def _open_its_own_session_id(project_id: ID, claim_id: ID) -> ID:
    """No role, no request, no turn: an eval's claim must not read as under review."""
    session_id: ID = open_session_store().create(
        title=f"Eval · claim {claim_id}",
        context={"project_id": project_id, "hidden": True},
    )
    return session_id


def _render_row(challenges: list[DraftChallenge], session_id: ID) -> ReviewedClaimRow:
    return {
        "challenge_count": len(challenges),
        "challenges_json": json.dumps([_describe_challenge(one) for one in challenges]),
        "review_session_id": session_id,
    }


def _describe_challenge(challenge: DraftChallenge) -> JsonDict:
    part = challenge.claim_part
    return {
        "kind": ChallengeKind(challenge.kind).value,
        "claim_part": part.phrase if part is not None else None,
        "claim_part_occurrence": part.occurrence if part is not None else None,
        "text": challenge.text,
        "justification": challenge.justification,
        "severity": Severity(challenge.severity).name,
    }


def _read_required_id(row: EvalCaseRow, column: str) -> ID:
    value = row.get(column)
    if value is None or not str(value).strip():
        raise ValueError(
            f"the row carries no {column!r}: a case names the project and the claim to review")
    return str(value)


def _read_model(row: EvalCaseRow, project_id: ID) -> str:
    named = row.get(MODEL_COLUMN)
    if named is not None and str(named).strip():
        return str(named)
    configured = project_meta(project_id).model
    if not configured:
        raise ValueError(
            f"no model to review with: the row carries no {MODEL_COLUMN!r} and project "
            f"{project_id!r} has none set")
    return configured
