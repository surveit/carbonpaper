"""The run header's view-model (app/web/run_header.py).

The run page states its status in no words at all — the reader gets it off the
one action offered. So which action a manifest selects IS the status display,
and there is one test here per state it can be in.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.web.config import templates
from app.web.run_header import (
    VersionNote,
    build_run_header,
    choose_run_cta,
    find_halted_stage_ids,
    format_duration,
    measure_elapsed_seconds,
    read_file_name,
    read_version_note,
)
from app.services import workspace

PROJECT = "hdr"
RUN = "20260730T120456"
BASE = f"/project/{PROJECT}/runs/{RUN}"


def _manifest(status: str, stages: list[tuple[str, str]], **extra: object) -> dict:
    return {
        "run_id": RUN, "started_at": "2026-07-30T12:04:56", "project": PROJECT,
        "workflow_version": None, "status": status,
        "human_review_queue_stats": {},
        "stage_records": [{"stage_id": sid, "status": st} for sid, st in stages],
        **extra,
    }


def _cta(manifest: dict):
    return choose_run_cta(PROJECT, RUN, manifest)


# ─── One state, one primary action ──────────────────────────────────────────

def test_a_running_run_offers_cancel_and_names_the_stage_it_is_on():
    cta = _cta(_manifest("running", [("load", "ok"), ("score", "running"),
                                     ("report", "pending")]))

    assert cta.primary is not None
    assert cta.primary.label == "✕ Cancel run"
    assert (cta.primary.url, cta.primary.method) == (f"{BASE}/cancel", "post")
    assert cta.aside == "on score — stage 2 of 3"
    assert cta.secondary == []


def test_a_halted_run_offers_the_review_queue_with_its_pending_count():
    manifest = _manifest(
        "awaiting_review",
        [("load", "ok"), ("review", "awaiting_review"), ("tail", "pending")],
        halted_at=["review"],
        human_review_queue_stats={"review": {"items_pending": 40}},
    )
    cta = _cta(manifest)

    assert cta.primary is not None
    assert cta.primary.label == "👤 Review 40 items in review →"
    assert cta.primary.url == f"{BASE}/queue/review"
    assert cta.primary.method == "get"
    # The aside says what deciding the queue STARTS. It used to read "1 stage waiting on
    # this", which frames a designed pause as an obstruction — the count is the same fact.
    assert cta.aside == "1 stage runs once this is decided"


def test_the_aside_counts_every_stage_the_decision_releases():
    manifest = _manifest(
        "awaiting_review",
        [("load", "ok"), ("review", "awaiting_review"), ("score", "pending"),
         ("report", "pending")],
        halted_at=["review"],
    )

    assert _cta(manifest).aside == "2 stages run once this is decided"


def test_a_failed_run_offers_a_re_run_that_keeps_the_completed_stages():
    cta = _cta(_manifest("errors", [("load", "ok"), ("tag", "validation_warnings"),
                                    ("score", "error"), ("rank", "error"),
                                    ("report", "pending")]))

    assert cta.primary is not None
    assert cta.primary.label == "↻ Re-run 2 failed stages →"
    assert (cta.primary.url, cta.primary.method) == (f"{BASE}/resume", "post")
    # The re-run reuses the completed stages' outputs, which is the whole reason
    # to press it rather than start a fresh run.
    assert cta.aside == "keeps the 2 completed stages — no new LLM calls"


def test_a_cancelled_run_offers_resume_not_a_re_run_of_failures():
    cta = _cta(_manifest("cancelled", [("load", "ok"), ("score", "cancelled"),
                                       ("report", "pending")]))

    assert cta.primary is not None
    assert cta.primary.label == "↻ Resume cancelled run →"
    assert (cta.primary.url, cta.primary.method) == (f"{BASE}/resume", "post")
    assert cta.aside == "keeps the 1 completed stage — no new LLM calls"


def test_a_completed_run_asks_for_nothing():
    cta = _cta(_manifest("ok", [("load", "ok"), ("report", "ok")]))

    # Its outputs are not an action: the run page lists them off header.artifacts, so
    # a long filename can no longer size a primary button.
    assert cta.primary is None
    assert cta.secondary == []
    assert cta.aside is None


def test_a_completed_run_that_published_nothing_offers_no_action_at_all():
    cta = _cta(_manifest("ok", [("load", "ok")]))

    assert cta.primary is None
    assert cta.secondary == []


# ─── Where two states overlap ───────────────────────────────────────────────

def test_a_run_both_halted_and_failed_leads_with_the_review():
    manifest = _manifest(
        "awaiting_review",
        [("load", "ok"), ("review", "awaiting_review"), ("score", "error")],
        halted_at=["review"],
    )
    cta = _cta(manifest)

    assert cta.primary is not None and cta.primary.url == f"{BASE}/queue/review"
    assert [a.label for a in cta.secondary] == ["↻ Re-run 1 failed stage →"]
    assert cta.secondary[0].kind == "ghost"


def test_every_halted_stage_gets_its_own_queue_action():
    manifest = _manifest(
        "awaiting_review",
        [("load", "ok"), ("review_a", "awaiting_review"), ("review_b", "awaiting_review")],
        halted_at=["review_a", "review_b"],
    )
    cta = _cta(manifest)

    assert cta.primary is not None and cta.primary.url == f"{BASE}/queue/review_a"
    assert [a.url for a in cta.secondary] == [f"{BASE}/queue/review_b"]


def test_a_halted_run_with_no_recorded_count_asks_without_inventing_one():
    manifest = _manifest("awaiting_review", [("review", "awaiting_review")],
                         halted_at=["review"])
    cta = _cta(manifest)

    assert cta.primary is not None
    assert cta.primary.label == "👤 Review items in review →"


def test_halted_stages_fall_back_to_the_stage_statuses_when_halted_at_is_absent():
    manifest = _manifest("awaiting_review",
                         [("load", "ok"), ("review", "awaiting_review")])
    assert find_halted_stage_ids(manifest) == ["review"]


# ─── The strip's counts stay honest about every status ──────────────────────

def test_the_counts_name_all_seven_stage_statuses(tmp_path: Path):
    workspace.set_projects_dir(tmp_path)
    (tmp_path / PROJECT / "runs" / RUN).mkdir(parents=True)
    manifest = _manifest("errors", [
        ("a", "ok"), ("b", "validation_warnings"), ("c", "running"),
        ("d", "awaiting_review"), ("e", "error"), ("f", "cancelled"),
        ("g", "pending"),
    ])

    header = build_run_header(PROJECT, RUN, tmp_path / PROJECT / "runs" / RUN, manifest)

    assert [(t.status, t.count) for t in header.strip.counts] == [
        ("ok", 1), ("validation_warnings", 1), ("running", 1),
        ("awaiting_review", 1), ("error", 1), ("cancelled", 1), ("pending", 1),
    ]
    # A pending stage's label says WHY it has not run, which is read off the run.
    assert header.strip.counts[-1].label == "not reached"


# ─── Provenance and duration: absent stays absent ───────────────────────────

def test_a_run_with_no_recorded_version_says_so_rather_than_showing_nothing():
    note = read_version_note(PROJECT, None)
    assert (note.version_id, note.message, note.error) == (None, None, None)


def test_an_unresolvable_version_carries_the_reason_not_a_blank_message(tmp_path: Path):
    workspace.set_projects_dir(tmp_path)
    (tmp_path / PROJECT).mkdir()

    note = read_version_note(PROJECT, "20990101T000000")

    assert note.version_id == "20990101T000000"
    assert note.message is None
    assert note.error is not None and "could not be read" in note.error


# ─── The version reads as what it says, not as its id ───────────────────────

VERSION_ID = "20260803T163854"
MESSAGE = "Registrations are logged and counted separately, not dropped"


def _render_version_line(tmp_path: Path, note: VersionNote) -> str:
    workspace.set_projects_dir(tmp_path)
    run_dir = tmp_path / PROJECT / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    header = build_run_header(PROJECT, RUN, run_dir, _manifest("ok", [("load", "ok")]))
    return templates.env.get_template("_run_header.html").render(
        project_id=PROJECT, run_id=RUN, header=header.model_copy(update={"version": note})
    )


def test_the_version_link_reads_as_its_message_not_its_timestamp_id(tmp_path: Path):
    html = _render_version_line(
        tmp_path, VersionNote(version_id=VERSION_ID, message=MESSAGE))

    assert f'/workflow/version/{VERSION_ID}">“{MESSAGE}”</a>' in html
    # The id is the href and nothing else — it is a key, not something to read.
    assert f"<code>{VERSION_ID}</code>" not in html


def test_a_version_carrying_no_message_stays_clickable_under_its_id(tmp_path: Path):
    html = _render_version_line(tmp_path, VersionNote(version_id=VERSION_ID))

    assert f'/workflow/version/{VERSION_ID}"><code>{VERSION_ID}</code></a>' in html


def test_an_unreadable_version_states_the_reason_beside_its_id(tmp_path: Path):
    html = _render_version_line(
        tmp_path,
        VersionNote(version_id=VERSION_ID, error="version could not be read: gone"))

    assert f'/workflow/version/{VERSION_ID}"><code>{VERSION_ID}</code></a>' in html
    assert "this version could not be read" in html


@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (48, "48s"), (134, "2m 14s"), (242, "4m 02s"), (3840, "1h 04m"),
])
def test_a_duration_reads_as_a_run_length(seconds: int, expected: str):
    assert format_duration(seconds) == expected


def test_a_finished_run_missing_its_end_timestamp_gets_no_duration():
    assert measure_elapsed_seconds("2026-07-30T12:04:56", None,
                                   still_running=False) is None


def test_an_unparseable_timestamp_gets_no_duration():
    assert measure_elapsed_seconds("not a time", "2026-07-30T12:05:56",
                                   still_running=False) is None


@pytest.mark.parametrize("started_at,finished_at", [
    ("2026-07-20T21:00:00+00:00", "2026-07-21T13:47:14"),
    ("2026-07-20T21:00:00", "2026-07-21T13:47:14+00:00"),
])
def test_one_timestamp_with_an_offset_and_one_without_gets_no_duration(
    started_at: str, finished_at: str
):
    assert measure_elapsed_seconds(started_at, finished_at,
                                   still_running=False) is None


def test_a_running_run_started_with_an_offset_still_gets_a_duration():
    seconds = measure_elapsed_seconds("2026-07-20T21:00:00+00:00", None,
                                      still_running=True)
    assert seconds is not None and seconds > 0


@pytest.mark.parametrize("path,expected", [
    (r"C:\Users\a\data\q2_filings.csv", "q2_filings.csv"),
    ("/tmp/proj/data/a.csv", "a.csv"),
    ("a.csv", "a.csv"),
])
def test_an_input_path_shows_as_its_file_name(path: str, expected: str):
    assert read_file_name(path) == expected
