"""One real attack on the fixture claim; deselected by default, opt in with -m live_llm."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

import app.compiler.claim_attack.run as claim_attack_run
from app.core.agent.store import SessionStore
from app.services import claim_review
from claim_review_fixture import PROJECT, claim_the_total, run_the_fixture

pytestmark = pytest.mark.live_llm

ATTACK_TIMEOUT = 600.0


def test_one_live_attack_on_the_fixture_claim(projects_root) -> None:
    claim = claim_the_total(run_the_fixture(projects_root))
    store = SessionStore()

    started = time.monotonic()
    parent_id = asyncio.run(_attack_the_claim(claim.id))
    elapsed = time.monotonic() - started

    print(f"\n=== wall clock: {elapsed:.1f}s ===")
    _print_session("PARENT", store.load(parent_id))
    _print_the_review(store, claim.id)

    assert store.load(parent_id)["active_turn"] is None, (
        "the attack never cleared the parent's active turn")


async def _attack_the_claim(claim_id: str) -> str:
    parent_id = claim_review.start_claim_attack(PROJECT, claim_id, model="sonnet")
    [task] = list(claim_attack_run._ATTACKS)
    await asyncio.wait_for(task, timeout=ATTACK_TIMEOUT)
    return parent_id


def _print_the_review(store: SessionStore, claim_id: str) -> None:
    review = claim_review.load_claim_review(PROJECT, claim_id)
    if review is None:
        print("\n=== NO REVIEW STORED ===")
        return

    print(f"\n=== SUMMARY ===\n{review.summary}")
    print(f"\n=== GROUNDING ({len(review.grounding)} phrases) ===")
    for index, phrase in enumerate(review.grounding):
        print(f"[{index}] {json.dumps(phrase.model_dump(mode='json'), ensure_ascii=False)}")
    print(f"\n=== CHALLENGES ({len(review.challenges)}) ===")
    for index, challenge in enumerate(review.challenges):
        print(f"[{index}] attacker={challenge.attacker} kind={challenge.kind} "
              f"severity={challenge.severity} grounding_index={challenge.grounding_index}")
        print(f"    backing: {challenge.backing!r}")
        print(f"    text: {challenge.text}")
        print(f"    evidence: {challenge.evidence}")
    print(f"\n=== REWRITES ({len(review.proposed_rewrites)}) ===")
    for rewrite in review.proposed_rewrites:
        print(f"    {rewrite.text!r} — {rewrite.why}")
    print(f"\n=== SESSIONS ({len(review.session_ids)}) ===")
    for session_id in review.session_ids:
        session = store.load(session_id)
        print(f"    {session_id}  {session['title']}  spend={_spend_of(session)}")


def _print_session(label: str, session: dict[str, Any]) -> None:
    print(f"\n=== {label}: {session['title']} ===")
    print(f"active_turn={session['active_turn']!r} spend={_spend_of(session)}")
    for message in session["messages"]:
        for part in message["parts"]:
            print(f"[{message['role']}] {part.get('text', part)}")


def _spend_of(session: dict[str, Any]) -> str:
    return json.dumps([spend["usage"] for spend in session["turn_spend"]])
