"""Behavior tests for the `queue_page` route (app/web/routers/review.py) — the
reviewer UI for one human_review_queue stage.

These tests characterize PRESERVED behavior through the public HTTP route
only: they never import queue_page's private module-level helpers and assert
only on the HTTP response (status code + rendered template output), never on
internals. Every assertion here would have passed just as well against the
pre-decomposition version of queue_page (a single complexity-43 function) —
this module pins behavior, not the new internal structure.

Projects are built on disk and run through the real runner
(app.runtime.runner.prepare_run / run_prepared) — the same pattern
tests/test_run_loop_semantics.py uses for human_review_queue halts — so the
queue snapshot and upstream output files queue_page reads are genuine runner
output, not hand-assembled fixtures. The llm_transform stage's model call is
mocked (deterministic score, no live LLM) via the same `lt.call_llm`
monkeypatch technique test_run_loop_semantics.py uses.
"""
from __future__ import annotations

import json

import pandas as pd
from fastapi.testclient import TestClient

import app.web.loading as loading
from app.main import app
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stages import llm_transform as lt
from app.services import versioning
from app.services.versioning import create_version_from_disk

PROJECT = "queue_page_journey"


def _seed_version(root):
    vid = create_version_from_disk(root, message="test seed", reviewer="test").version_id
    versioning.publish_version(root, vid, reviewer="human")


def _write_stage(root, filename, stage):
    (root / "compiled").mkdir(parents=True, exist_ok=True)
    (root / "compiled" / filename).write_text(json.dumps(stage), encoding="utf-8")


def _load_quotes_stage(root):
    """input_data stage reading a 2-row (id, quote) csv — the MODEL INPUT the
    scoring stage judges. `review`'s own queued row does NOT carry `quote`
    (see `_review_stage`), so the only way it can appear on the page is via
    queue_page's join-back-to-upstream recovery."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "quotes.csv"
    pd.DataFrame({
        "id": ["a", "b"],
        "quote": ["Quote about widgets.", "Quote about gadgets."],
    }).to_csv(csv_path, index=False)
    return {"id": "load", "name": "Load quotes", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}}}


def _score_stage():
    """llm_transform: scores each quote. output_schema is additive (a stage
    invariant — app/models/stage.py's _llm_transform_one_to_one), so `quote`
    survives onto the queued row; the prompt_data_template references
    `{quote}` so a successful model-input recovery can render the exact
    prompt sent."""
    return {"id": "score", "name": "Score quotes", "type": "llm_transform",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "quote", "type": "str"}],
                "primary_key": ["id"]}}],
            "output_schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "quote", "type": "str"},
                            {"name": "score", "type": "int", "nullable": False}],
                "primary_key": ["id"]},
            "llm": {"prompt_instructions": "Score each quote for tone.",
                    "prompt_data_template": "Rate this: {quote}"}}


def _review_stage():
    """human_review_queue reviewing `score`'s output; no prior decisions on
    disk, so the run halts and snapshots both rows."""
    return {"id": "review", "name": "Review scores", "type": "human_review_queue",
            "inputs": [{"id": "score", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "quote", "type": "str"},
                            {"name": "score", "type": "int"}],
                "primary_key": ["id"]}}],
            "queue": {"hash_columns": ["id"]}}


def _build_and_halt(tmp_path, monkeypatch):
    """Builds load -> score (llm_transform, mocked) -> review
    (human_review_queue) and runs it for real. The run halts at `review` with
    both rows snapshotted. Returns (run_id, run_dir, queue_snapshot_df)."""
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(
        lt, "call_llm", lambda stage_id, llm_config, row, **kw: {"score": 1}
    )

    project_dir = tmp_path / PROJECT
    _write_stage(project_dir, "01_load.json", _load_quotes_stage(project_dir))
    _write_stage(project_dir, "02_score.json", _score_stage())
    _write_stage(project_dir, "03_review.json", _review_stage())
    _seed_version(project_dir)

    manifest = run_prepared(prepare_run(project_dir, repo_root=project_dir))
    assert manifest["status"] == "awaiting_review"
    assert manifest["halted_at"] == ["review"]

    run_dir = project_dir / "runs" / manifest["run_id"]
    snapshot = pd.read_parquet(run_dir / "queue" / "review.parquet")
    return manifest["run_id"], run_dir, snapshot


def _write_decision(stage_id: str, content_hash: str, decision: str, run_id: str) -> None:
    path = loading.decisions_path(PROJECT, stage_id)
    pd.DataFrame([{
        "content_hash": content_hash, "decision": decision, "modified_score": None,
        "reviewer": "local", "reviewed_at": "2026-07-01T00:00:00", "source_run_id": run_id,
    }]).to_parquet(path, index=False)


# ── 1. Happy path: snapshot + prior decisions ────────────────────────────────


def test_happy_path_renders_items_with_hash_prior_decision_and_counts(tmp_path, monkeypatch):
    run_id, _run_dir, snapshot = _build_and_halt(tmp_path, monkeypatch)
    first_hash, second_hash = snapshot["content_hash"].tolist()
    _write_decision("review", first_hash, "approve", run_id)

    client = TestClient(app)
    r = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review")

    assert r.status_code == 200
    html = r.text
    # Both queued rows surface their content_hash — the join key /decide posts against.
    assert f'data-content-hash="{first_hash}"' in html
    assert f'data-content-hash="{second_hash}"' in html
    # The decided row carries its prior decision; the other does not.
    assert html.count("decided-approve") == 1
    assert "<strong>approve</strong>" in html
    # reviewed_count/total: exactly one of two rows has a prior decision.
    assert "<strong>1</strong> of <strong>2</strong> reviewed" in html


# ── 2. Model-input recovery: resolvable join keys ────────────────────────────


def test_model_input_recovery_renders_the_exact_rendered_prompt(tmp_path, monkeypatch):
    run_id, _run_dir, _snapshot = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    html = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    # The queued row itself carries no `quote` (see _score_stage's docstring);
    # this text can only appear via a successful join back to the `load`
    # stage's output, rendered into the prompt the model actually received.
    assert '<pre class="prompt-rendered">' in html
    assert "Rate this: Quote about widgets." in html
    assert "Rate this: Quote about gadgets." in html


# ── 3. Degraded path: upstream scored-input table missing on disk ───────────


def test_degrades_gracefully_when_upstream_scored_input_is_missing(tmp_path, monkeypatch):
    run_id, run_dir, snapshot = _build_and_halt(tmp_path, monkeypatch)

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    load_record = next(s for s in manifest["stages"] if s["stage_id"] == "load")
    (run_dir / load_record["output_path"]).unlink()  # the frame model_input would join against

    client = TestClient(app)
    r = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review")

    assert r.status_code == 200  # missing upstream output does not break the page
    html = r.text
    # Items still render — both content hashes present.
    for content_hash in snapshot["content_hash"]:
        assert f'data-content-hash="{content_hash}"' in html
    # No rendered prompt (needs model_input) and no raw model-input dump (also
    # needs model_input): both of queue_page's model_input-gated blocks are
    # absent, evidencing model_input/rendered_prompt are None for every item.
    assert '<pre class="prompt-rendered">' not in html
    assert "model input — all fields" not in html


# ── 4. 404 on a stage that isn't a human_review_queue stage ─────────────────


def test_404_when_the_stage_id_is_not_a_human_review_queue_stage(tmp_path, monkeypatch):
    run_id, _run_dir, _snapshot = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    r = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/load")  # `load` is input_data

    assert r.status_code == 404
