"""The run log's SSE tail (GET /project/{p}/runs/{id}/events).

One code path serves the live feed and a finished run: drain the run's stored
events, end on the terminal run_done marker — or, if it never arrived, once the
manifest settled, so an interrupted run can't hang a client.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.run_status import RunStatus
from app.main import app
from app.runtime.context import RunContext
from app.runtime.manifest import create_run_manifest, write_manifest
from app.runtime.run_log import RUN_DONE
from fastapi.testclient import TestClient
from app.services import workspace
from run_seed import store_events

PROJECT = "events_stream"


def _seed_run(tmp_path: Path, monkeypatch, events: list[dict]) -> str:
    workspace.set_projects_dir(tmp_path)
    run_dir = tmp_path / PROJECT / "runs" / "r1"
    run_dir.mkdir(parents=True)
    manifest = create_run_manifest(
        [], RunContext(run_dir=run_dir),
        run_id="r1", project_id=PROJECT, workflow_version=None,
        input_bindings={},
    )
    manifest.status = RunStatus.OK
    write_manifest(manifest)
    store_events(PROJECT, "r1", events)
    return f"/project/{PROJECT}/runs/r1/events"


def _streamed_kinds(body: str) -> list[str]:
    return [
        json.loads(line[len("data: "):])["kind"]
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: {}"
    ]


def test_a_finished_run_drains_and_ends_on_the_run_done_marker(tmp_path, monkeypatch):
    url = _seed_run(tmp_path, monkeypatch, [
        {"seq": 0, "kind": "run_start", "level": 0},
        {"seq": 1, "kind": "row_ok", "stage": "s", "row": 0, "level": 0},
        {"seq": 2, "kind": RUN_DONE, "level": 0},
    ])

    response = TestClient(app).get(url)

    assert response.status_code == 200
    assert _streamed_kinds(response.text) == ["run_start", "row_ok", RUN_DONE]


def test_from_seq_resumes_after_a_reconnect(tmp_path, monkeypatch):
    url = _seed_run(tmp_path, monkeypatch, [
        {"seq": 0, "kind": "run_start", "level": 0},
        {"seq": 1, "kind": "row_ok", "stage": "s", "row": 0, "level": 0},
        {"seq": 2, "kind": RUN_DONE, "level": 0},
    ])

    response = TestClient(app).get(url, params={"from_seq": 2})

    assert _streamed_kinds(response.text) == [RUN_DONE]


def test_an_interrupted_run_ends_the_stream_instead_of_hanging(tmp_path, monkeypatch):
    url = _seed_run(tmp_path, monkeypatch, [
        {"seq": 0, "kind": "run_start", "level": 0},
    ])

    response = TestClient(app).get(url)

    assert _streamed_kinds(response.text) == ["run_start"]
    assert "event: done" in response.text


def _lifecycle_events(count: int) -> list[dict]:
    return [
        {"seq": i, "kind": "row_ok", "stage": "s", "row": i, "level": 0}
        for i in range(count - 1)
    ] + [{"seq": count - 1, "kind": RUN_DONE, "level": 0}]


def test_a_long_log_opens_on_the_tail_rather_than_replaying_all_of_it(
    tmp_path, monkeypatch
):
    """A large stage logs hundreds of thousands of events — replaying them all froze the panel."""
    url = _seed_run(tmp_path, monkeypatch, _lifecycle_events(1200))

    response = TestClient(app).get(url, params={"tail": 100})

    streamed = [
        json.loads(line[len("data: "):])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: {}"
    ]
    assert len(streamed) == 100
    assert streamed[0]["seq"] == 1100          # the LAST 100, not the first
    assert streamed[-1]["kind"] == RUN_DONE


def test_an_explicit_from_seq_still_wins_over_the_tail_default(tmp_path, monkeypatch):
    url = _seed_run(tmp_path, monkeypatch, _lifecycle_events(1200))

    response = TestClient(app).get(url, params={"from_seq": 0, "tail": 100})

    assert len(_streamed_kinds(response.text)) == 1200


def test_load_older_pages_backwards_from_a_cursor(tmp_path, monkeypatch):
    _seed_run(tmp_path, monkeypatch, _lifecycle_events(1200))

    response = TestClient(app).get(
        f"/project/{PROJECT}/runs/r1/events/page",
        params={"before_seq": 1100, "limit": 100},
    )

    page = response.json()
    assert [e["seq"] for e in page["events"]] == list(range(1000, 1100))
    assert page["first_seq"] == 1000
    assert page["has_more"] is True


def test_the_last_page_back_reports_that_nothing_older_remains(tmp_path, monkeypatch):
    _seed_run(tmp_path, monkeypatch, _lifecycle_events(1200))

    response = TestClient(app).get(
        f"/project/{PROJECT}/runs/r1/events/page",
        params={"before_seq": 40, "limit": 100},
    )

    page = response.json()
    assert [e["seq"] for e in page["events"]] == list(range(40))
    assert page["has_more"] is False


def _two_stage_events() -> list[dict]:
    return [
        {"seq": 0, "kind": "run_start", "level": 0},
        {"seq": 1, "kind": "row_ok", "stage": "load", "row": 0, "level": 0},
        {"seq": 2, "kind": "row_ok", "stage": "classify", "row": 0, "level": 0},
        {"seq": 3, "kind": "row_error", "stage": "load", "row": 1, "level": 0},
        {"seq": 4, "kind": RUN_DONE, "level": 0},
    ]


def test_a_stage_scoped_feed_carries_only_that_stage_and_the_end_marker(
    tmp_path, monkeypatch
):
    url = _seed_run(tmp_path, monkeypatch, _two_stage_events())

    response = TestClient(app).get(url, params={"stage": "load"})

    streamed = [
        json.loads(line[len("data: "):])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: {}"
    ]
    assert [e["seq"] for e in streamed] == [1, 3, 4]


def test_the_stage_tail_is_counted_over_that_stage_s_own_events(tmp_path, monkeypatch):
    events = [
        {"seq": i, "kind": "row_ok", "stage": "noisy", "row": i, "level": 0}
        for i in range(1000)
    ]
    events += [
        {"seq": 1000, "kind": "row_ok", "stage": "quiet", "row": 0, "level": 0},
        {"seq": 1001, "kind": "row_ok", "stage": "noisy", "row": 1000, "level": 0},
        {"seq": 1002, "kind": RUN_DONE, "level": 0},
    ]
    url = _seed_run(tmp_path, monkeypatch, events)

    response = TestClient(app).get(url, params={"stage": "quiet", "tail": 10})

    streamed = [
        json.loads(line[len("data: "):])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: {}"
    ]
    assert [e["seq"] for e in streamed] == [1000, 1002]


def test_load_older_pages_over_the_filtered_events_not_a_seq_window(
    tmp_path, monkeypatch
):
    events = [
        {"seq": i, "kind": "row_ok", "stage": "quiet" if i % 100 == 0 else "noisy",
         "row": i, "level": 0}
        for i in range(1000)
    ]
    _seed_run(tmp_path, monkeypatch, events)

    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/r1/events/page",
        params={"before_seq": 900, "limit": 5, "stage": "quiet"},
    ).json()

    # The last 5 "quiet" events older than seq 900 — not the none of them that
    # fall inside the seq 895..899 window a subtraction would have taken.
    assert [e["seq"] for e in page["events"]] == [400, 500, 600, 700, 800]
    assert page["has_more"] is True


def test_an_unknown_run_is_a_404(tmp_path, monkeypatch):
    _seed_run(tmp_path, monkeypatch, [])

    response = TestClient(app).get(f"/project/{PROJECT}/runs/nope/events")

    assert response.status_code == 404
