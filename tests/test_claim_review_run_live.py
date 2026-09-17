"""One real review of the fixture claim; deselected by default, opt in with -m live_llm."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

import app.reviewer.run as reviewer_run
from app.core.agent.store import SessionStore
from app.services import claim_review, claim_review_run
from claim_review_fixture import PROJECT, claim_the_total, run_the_fixture

pytestmark = pytest.mark.live_llm

REVIEW_TIMEOUT = 600.0


def test_one_live_review_of_the_fixture_claim(projects_root) -> None:
    claim = claim_the_total(run_the_fixture(projects_root))
    store = SessionStore()

    started = time.monotonic()
    session_id = asyncio.run(_review_the_claim(claim.id))
    elapsed = time.monotonic() - started

    print(f"\n=== wall clock: {elapsed:.1f}s ===")
    _print_session(store.load(session_id))
    _print_the_review(store, claim.id)

    assert store.load(session_id)["active_turn"] is None, (
        "the review never cleared its active turn")


async def _review_the_claim(claim_id: str) -> str:
    session_id = claim_review_run.start_claim_review(PROJECT, claim_id, model="sonnet")
    [task] = list(reviewer_run._REVIEWS)
    await asyncio.wait_for(task, timeout=REVIEW_TIMEOUT)
    return session_id


def _print_the_review(store: SessionStore, claim_id: str) -> None:
    review = claim_review.load_claim_review(claim_id)
    if review is None:
        print("\n=== NO REVIEW STORED ===")
        return

    print(f"\n=== CHALLENGES ({len(review.challenges)}) ===")
    for index, challenge in enumerate(review.challenges):
        lands_on = challenge.claim_part.phrase if challenge.claim_part else "the whole sentence"
        print(f"[{index}] kind={challenge.kind} severity={challenge.severity} on {lands_on!r}")
        print(f"    text: {challenge.text}")
        print(f"    justification: {challenge.justification}")
        for citation in challenge.citations:
            print(f"    cites: {json.dumps(citation.model_dump(mode='json'))}")
    session = store.load(review.session_id)
    print(f"\n=== SESSION {review.session_id} ===  spend={_spend_of(session)}")


def _print_session(session: dict[str, Any]) -> None:
    print(f"\n=== {session['title']} ===")
    print(f"active_turn={session['active_turn']!r} spend={_spend_of(session)}")
    for message in session["messages"]:
        for part in message["parts"]:
            print(f"[{message['role']}] {part.get('text', part)}")


def _spend_of(session: dict[str, Any]) -> str:
    return json.dumps([spend["usage"] for spend in session["turn_spend"]])
