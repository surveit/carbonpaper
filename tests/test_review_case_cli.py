"""`python -m app.review_case <case_dir>`: what it prints, and what it refuses to print."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from app.core.agent.store import open_session_store
from app.evals.case import CASE_FILE
from app.models.records.claim_review import Challenge, ChallengeKind, ClaimReview, Severity
from app.models.records.claims import Claim
from app.models.records.project import Project
from app.review_case import ARCHIVE_FILE, POLL_SECONDS, main
from app.services import run as run_service
from app.services.claim_shapes import write_claim_shapes
from app.services.methodology import write_methodology
from app.services.project import export_project_archive, save_working_copy_as_version
from claim_review_fixture import PROJECT, TOTAL_SHAPE, TOTAL_TEXT, add_a_sandboxed_filter
from run_seed import list_run_ids
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

_SUMMARY = "the figure is a share, not a count"
_SLUG = "grant-total"


def _a_fake_review(project_id: str, claim_id: str, *, model: str) -> str:
    ClaimReview(
        claim_id=claim_id, session_id="fake-session",
        challenges=[Challenge(kind=ChallengeKind.gap, text=_SUMMARY,
                              justification="the run counts rows", severity=Severity.major)],
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


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _declare_the_total_as_an_output(specs: list[dict], shape_id: str) -> list[dict]:
    """The run itself publishes the slug the case claims; a claim needs no seeding."""
    for spec in specs:
        if spec["id"] == "grant_totals":
            spec["workflow_outputs"] = [{
                "kind": "figure", "slug": _SLUG, "label": "What the grants came to",
                "primary": True, "column": "total_amount", "shape_id": shape_id}]
    return specs


@pytest.fixture
def seeded_case_dir(tmp_path, projects_root) -> Path:
    case_dir = tmp_path / "case"
    sources = case_dir / "sources"
    (projects_root / PROJECT).mkdir(parents=True, exist_ok=True)
    Project(id=PROJECT, name=PROJECT, model="claude-sonnet-5", source="fixture").save()
    write_methodology(PROJECT, "How the grants were totalled.")
    write_inputs(sources)
    [shape] = write_claim_shapes(PROJECT, [TOTAL_SHAPE])
    set_stages(PROJECT, _declare_the_total_as_an_output(
        add_a_sandboxed_filter(stage_specs(sources)), shape.id))
    save_working_copy_as_version(PROJECT, message="fixture")
    run_service.execute(PROJECT)
    (case_dir / ARCHIVE_FILE).write_bytes(export_project_archive(PROJECT))
    (case_dir / CASE_FILE).write_text(json.dumps({
        "output_slug": _SLUG, "claim_context": {}, "claim_text": TOTAL_TEXT,
        "model": "claude-sonnet-5", "expected_outputs": [_SLUG],
        "sources": [{"path": f"sources/{name}", "sha256": _digest(sources / name)}
                    for name in sorted(path.name for path in sources.iterdir())],
    }), encoding="utf-8")
    return case_dir


def test_a_replayable_case_submits_its_claim_and_prints_the_challenges(
    capsys, seeded_case_dir, monkeypatch
):
    monkeypatch.setattr("app.review_case.start_claim_review", _a_fake_review)

    exit_code = main([str(seeded_case_dir)])

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    # Minted by submit_claim against the replayed run: the case carried no claim id.
    assert printed["claim_id"]
    assert [challenge["text"] for challenge in printed["challenges"]] == [_SUMMARY]


def test_the_claim_is_submitted_against_the_replayed_run_not_the_captured_one(
    capsys, seeded_case_dir, monkeypatch
):
    monkeypatch.setattr("app.review_case.start_claim_review", _a_fake_review)
    captured = set(list_run_ids(PROJECT))

    main([str(seeded_case_dir)])

    claim = Claim.load(json.loads(capsys.readouterr().out)["claim_id"])
    assert claim.text == TOTAL_TEXT
    assert captured and claim.citation.run_id not in captured


def test_a_drifted_source_is_refused_before_anything_runs(capsys, seeded_case_dir):
    (seeded_case_dir / "sources" / "east.csv").write_bytes(b"x,y\n9,9\n")

    exit_code = main([str(seeded_case_dir)])

    assert exit_code == 1
    assert "differ from what this case captured" in capsys.readouterr().out


def test_a_review_that_stored_nothing_is_refused_rather_than_printed_empty(
    capsys, seeded_case_dir, monkeypatch
):
    monkeypatch.setattr("app.review_case.start_claim_review", _a_review_that_never_stores)

    exit_code = main([str(seeded_case_dir)])

    assert exit_code == 1
    printed = capsys.readouterr().out
    assert "stor" in printed and '"challenges"' not in printed


def test_a_review_that_lands_after_a_poll_is_waited_for(capsys, seeded_case_dir, monkeypatch):
    monkeypatch.setattr("app.review_case.start_claim_review", _a_review_that_lands_late)

    exit_code = main([str(seeded_case_dir)])

    assert exit_code == 0
    assert [one["text"] for one in json.loads(capsys.readouterr().out)["challenges"]] == [_SUMMARY]
