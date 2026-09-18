"""python -m app.review_case <case_dir> — replay a captured case and review its claim."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.core.agent.store import AgentSession
from app.core.event_loop import create_event_loop, validate_running_loop_can_spawn_subprocesses
from app.core.ids import ID
from app.core.store_config import configure_default_stores, refuse_renamed_env_vars
from app.evals.case import Case, read_case
from app.evals.errors import CaseDidNotReplay, CaseInvalid
from app.evals.replay import validate_run_called_no_model, validate_sources_match_capture
from app.services.claim_review import load_claim_review
from app.services.claim_review_run import start_claim_review
from app.services.errors import ClaimReviewRefused
from app.services.project import import_project_archive
from app.services.run import execute, load_run_workflow
from app.services.workspace import configure_projects_dir_from_env

ARCHIVE_FILE = "project.zip"

REVIEW_DEADLINE_SECONDS = 900.0

POLL_SECONDS = 0.5


def main(argv: list[str] | None = None) -> int:
    case_dir = _parse_args(argv).case_dir
    refuse_renamed_env_vars()
    configure_projects_dir_from_env()
    configure_default_stores()
    try:
        reviewed = _review_one_case(case_dir)
    except (CaseInvalid, CaseDidNotReplay, ClaimReviewRefused) as refusal:
        print(refusal)
        return 1
    print(json.dumps(reviewed, indent=2))
    return 0


def _review_one_case(case_dir: Path) -> dict[str, object]:
    case = read_case(case_dir)
    validate_sources_match_capture(case_dir, case)
    project_id = import_project_archive((case_dir / ARCHIVE_FILE).read_bytes()).project_id
    manifest = execute(project_id)
    validate_run_called_no_model(
        project_id, str(manifest["run_id"]), load_run_workflow(project_id, manifest))
    return _run_the_review(project_id, case)


def _run_the_review(project_id: ID, case: Case) -> dict[str, object]:
    loop = create_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_await_the_review(project_id, case))
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    return _read_the_stored_review(project_id, case.claim_id)


async def _await_the_review(project_id: ID, case: Case) -> None:
    validate_running_loop_can_spawn_subprocesses()
    session_id = start_claim_review(project_id, case.claim_id, model=str(case.model))
    await _await_the_review_landing(session_id, case.claim_id)


async def _await_the_review_landing(session_id: ID, claim_id: ID) -> None:
    deadline = asyncio.get_running_loop().time() + REVIEW_DEADLINE_SECONDS
    while True:
        # Read first: a review stored between the two reads is still found below.
        still_running = _read_whether_the_review_still_runs(session_id)
        if load_claim_review(claim_id) is not None:
            return
        if not still_running:
            raise ClaimReviewRefused(
                [f"the review of claim {claim_id} finished without storing a review"])
        if asyncio.get_running_loop().time() >= deadline:
            raise ClaimReviewRefused(
                [f"the review of claim {claim_id} stored no review within "
                 f"{REVIEW_DEADLINE_SECONDS:.0f}s"])
        await asyncio.sleep(POLL_SECONDS)


def _read_whether_the_review_still_runs(session_id: ID) -> bool:
    session = AgentSession.load_or_none(session_id)
    return session is not None and session.active_turn is not None


def _read_the_stored_review(project_id: ID, claim_id: ID) -> dict[str, object]:
    review = load_claim_review(claim_id)
    if review is None:
        raise ClaimReviewRefused([f"claim {claim_id} stored no review"])
    return {"project_id": project_id, "claim_id": claim_id,
            "challenges": [one.model_dump(mode="json") for one in review.challenges]}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.review_case",
        description="Review a captured case's claim, replaying its run.")
    parser.add_argument("case_dir", type=Path, help="the case directory holding case.json")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
