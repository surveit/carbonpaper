"""`python -m app.evals.review_case <case_dir>`: what it prints, and what it refuses to print."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core.agent.store import open_session_store
from app.evals.case import CASE_FILE
from app.models.records.claim_review import Challenge, ChallengeKind, ClaimReview, Severity
from app.models.records.claims import Claim
from app.models.records.project import Project
from app.evals.review_case import ARCHIVE_FILE, POLL_SECONDS, main
from app.services import run as run_service
from app.services.claim_shapes import write_claim_shapes
from app.services.methodology import write_methodology
from app.services.project import export_project_archive
from claim_review_fixture import PROJECT, TOTAL_SHAPE, TOTAL_TEXT, add_a_sandboxed_filter
from run_seed import list_run_ids
from scope_fixture import column, review_tail, stage_specs, write_inputs
from stage_seed import save_version, set_stages

_SUMMARY = "the figure is a share, not a count"
_SLUG = "grant-total"


def _a_fake_review(project_id: str, claim_id: str, *, model: str) -> str:
    ClaimReview(
        claim_id=claim_id, session_id="fake-session",
        challenges=[Challenge(kind=ChallengeKind.gap, text=_SUMMARY,
                              justification="the run counts rows", severity=Severity.high)],
    ).save()
    return "fake-session"


def _a_review_that_never_stores(project_id: str, claim_id: str, *, model: str) -> str:
    return "fake-session"


def _a_review_that_lands_late(project_id: str, claim_id: str, *, model: str) -> str:
    """Stores on a task under an open turn, the way the real reviewers do."""
    store = open_session_store()
    session_id = store.create(title="late", agent_id=None, context={})
    store.set_active_turn(session_id, "review")

    async def store_it_later() -> None:
        await asyncio.sleep(POLL_SECONDS * 2)
        _a_fake_review(project_id, claim_id, model=model)
        store.set_active_turn(session_id, None)

    asyncio.get_running_loop().create_task(store_it_later())
    return session_id


_ORPHANS: list[asyncio.Task] = []


def _a_review_that_keeps_running(project_id: str, claim_id: str, *, model: str) -> str:
    """The shape of a real reviewer: a task holding a `claude` subprocess, storing nothing."""
    async def keep_running() -> None:
        await asyncio.sleep(3600)

    _ORPHANS.append(asyncio.get_running_loop().create_task(keep_running()))
    return "fake-session"


def _declare_the_total_as_an_output(specs: list[dict], shape_id: str) -> list[dict]:
    """The run itself publishes the slug the case claims; a claim needs no seeding."""
    for spec in specs:
        if spec["id"] == "grant_totals":
            spec["workflow_outputs"] = [{
                "kind": "figure", "slug": _SLUG, "label": "What the grants came to",
                "primary": True, "column": "total_amount", "shape_id": shape_id}]
    return specs


def _a_judging_stage(*, cache: bool) -> dict:
    return {
        "id": "judge", "type": "llm_transform", "cache": cache,
        "description": "What the model made of each grant.",
        "inputs": [{"id": "grants_only"}],
        "signature": {"form": "extends", "reads": [
            {"input": "grants_only", "columns": [column("grant_id", "str", False)]}],
            "adds": [column("verdict", "str")], "rewrites": []},
        "llm": {"prompt_instructions": "judge it", "prompt_data_template": "{grant_id}",
                "batch_size": 1},
    }


def _a_stubbed_verdict(*args, **kwargs) -> dict:
    return {"verdict": "sound"}


@pytest.fixture
def seeded_case_dir(tmp_path, projects_root, monkeypatch) -> Path:
    return capture_a_case(tmp_path, projects_root, monkeypatch)


def capture_a_case(tmp_path, projects_root, monkeypatch, *, judged_cache=True, tail=()) -> Path:
    """Runs the fixture with the model stubbed, so the archive carries a warmed cache."""
    case_dir = tmp_path / "case"
    sources = case_dir / "sources"
    (projects_root / PROJECT).mkdir(parents=True, exist_ok=True)
    Project(id=PROJECT, name=PROJECT, model="claude-sonnet-5", source="fixture").save()
    write_methodology(PROJECT, "How the grants were totalled.")
    write_inputs(sources)
    [shape] = write_claim_shapes(PROJECT, [TOTAL_SHAPE])
    set_stages(PROJECT, [*_declare_the_total_as_an_output(
        add_a_sandboxed_filter(stage_specs(sources)), shape.id),
        _a_judging_stage(cache=judged_cache), *tail])
    save_version(PROJECT, message="fixture")
    monkeypatch.setattr("app.runtime.stages.llm_transform.call_llm", _a_stubbed_verdict)
    run_service.execute(PROJECT)
    monkeypatch.undo()
    (case_dir / ARCHIVE_FILE).write_bytes(export_project_archive(PROJECT))
    (case_dir / CASE_FILE).write_text(json.dumps({
        "output_slug": _SLUG, "claim_context": {}, "claim_text": TOTAL_TEXT,
        "model": "claude-sonnet-5", "expected_outputs": [_SLUG],
    }), encoding="utf-8")
    return case_dir


def test_a_replayable_case_submits_its_claim_and_prints_the_challenges(
    capsys, seeded_case_dir, monkeypatch
):
    monkeypatch.setattr("app.evals.review_case.start_claim_review", _a_fake_review)

    exit_code = main([str(seeded_case_dir)])

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    # Minted by submit_claim against the replayed run: the case carried no claim id.
    assert printed["claim_id"]
    assert [challenge["text"] for challenge in printed["challenges"]] == [_SUMMARY]


def test_the_claim_is_submitted_against_the_replayed_run_not_the_captured_one(
    capsys, seeded_case_dir, monkeypatch
):
    monkeypatch.setattr("app.evals.review_case.start_claim_review", _a_fake_review)
    captured = set(list_run_ids(PROJECT))

    main([str(seeded_case_dir)])

    claim = Claim.load(json.loads(capsys.readouterr().out)["claim_id"])
    assert claim.text == TOTAL_TEXT
    assert captured and claim.citation.run_id not in captured


def test_a_review_that_stored_nothing_is_refused_rather_than_printed_empty(
    capsys, seeded_case_dir, monkeypatch
):
    monkeypatch.setattr("app.evals.review_case.start_claim_review", _a_review_that_never_stores)

    exit_code = main([str(seeded_case_dir)])

    assert exit_code == 1
    printed = capsys.readouterr().out
    assert "stor" in printed and '"challenges"' not in printed


def test_a_review_that_lands_after_a_poll_is_waited_for(capsys, seeded_case_dir, monkeypatch):
    monkeypatch.setattr("app.evals.review_case.start_claim_review", _a_review_that_lands_late)

    exit_code = main([str(seeded_case_dir)])

    assert exit_code == 0
    assert [one["text"] for one in json.loads(capsys.readouterr().out)["challenges"]] == [_SUMMARY]


def test_the_model_stage_replays_from_the_imported_cache(capsys, seeded_case_dir, monkeypatch):
    """`call_llm` is left unstubbed: a cache miss would raise rather than bill a call."""
    monkeypatch.setattr("app.evals.review_case.start_claim_review", _a_fake_review)

    exit_code = main([str(seeded_case_dir)])

    assert exit_code == 0, capsys.readouterr().out


def test_a_model_stage_that_computed_a_row_is_refused_by_name(
    capsys, tmp_path, projects_root, monkeypatch
):
    case_dir = capture_a_case(tmp_path, projects_root, monkeypatch, judged_cache=False)
    monkeypatch.setattr("app.runtime.stages.llm_transform.call_llm", _a_stubbed_verdict)
    monkeypatch.setattr("app.evals.review_case.start_claim_review", _a_fake_review)

    exit_code = main([str(case_dir)])

    printed = capsys.readouterr().out
    assert exit_code == 1
    assert "judge" in printed and '"challenges"' not in printed


def test_a_run_that_did_not_finish_is_refused_rather_than_scored(
    capsys, tmp_path, projects_root, monkeypatch
):
    case_dir = capture_a_case(tmp_path, projects_root, monkeypatch, tail=review_tail())
    monkeypatch.setattr("app.evals.review_case.start_claim_review", _a_fake_review)

    exit_code = main([str(case_dir)])

    printed = capsys.readouterr().out
    assert exit_code == 1
    assert "awaiting_review" in printed and '"challenges"' not in printed


def test_a_case_without_its_archive_is_refused_by_path(capsys, seeded_case_dir):
    (seeded_case_dir / ARCHIVE_FILE).unlink()

    exit_code = main([str(seeded_case_dir)])

    assert exit_code == 1
    assert ARCHIVE_FILE in capsys.readouterr().out


def test_a_refused_case_leaves_no_reviewer_task_running(capsys, seeded_case_dir, monkeypatch):
    monkeypatch.setattr("app.evals.review_case.start_claim_review", _a_review_that_keeps_running)
    _ORPHANS.clear()

    exit_code = main([str(seeded_case_dir)])

    assert exit_code == 1
    assert [task.cancelled() for task in _ORPHANS] == [True]
