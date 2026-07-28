"""The run log's SSE tail (GET /project/{p}/runs/{id}/events).

One code path serves the live feed and a finished run: the endpoint drains
runs/<id>/events.jsonl and ends on the terminal run_done marker — or, when that
marker never arrived, once the manifest has settled, so an interrupted run can
never hang the client.
"""
from __future__ import annotations

import json
from pathlib import Path

import app.web.loading as loading
from app.core.run_status import RunStatus
from app.main import app
from app.runtime.manifest import create_run_manifest, write_manifest
from app.runtime.run_log import RUN_DONE
from fastapi.testclient import TestClient

PROJECT = "events_stream"


def _seed_run(tmp_path: Path, monkeypatch, events: list[dict]) -> str:
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    run_dir = tmp_path / PROJECT / "runs" / "r1"
    run_dir.mkdir(parents=True)
    manifest = create_run_manifest(
        [], run_id="r1", project=PROJECT, workflow_version=None,
        run_bindings={}, input_bindings={}, limits={}, offsets={}, bust_cache=False,
    )
    manifest.status = RunStatus.OK
    write_manifest(run_dir, manifest)
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
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
    # No run_done marker: the writer died mid-run. The manifest is terminal, so
    # the tail gives up rather than polling a file nothing will ever append to.
    url = _seed_run(tmp_path, monkeypatch, [
        {"seq": 0, "kind": "run_start", "level": 0},
    ])

    response = TestClient(app).get(url)

    assert _streamed_kinds(response.text) == ["run_start"]
    assert "event: done" in response.text


def test_an_unknown_run_is_a_404(tmp_path, monkeypatch):
    _seed_run(tmp_path, monkeypatch, [])

    response = TestClient(app).get(f"/project/{PROJECT}/runs/nope/events")

    assert response.status_code == 404
