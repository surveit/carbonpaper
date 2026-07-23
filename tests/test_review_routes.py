"""Behavior tests for the reviewer web routes (app/web/routers/review.py):
`queue_page` (GET) and `queue_decide` (POST) for one human_review_queue stage.

Both routes go through the stage-result cache (app.services.stage_cache),
never a `decisions/*.parquet` file: `queue_page`'s prior decisions come from
`StageCacheEntry.find_entries`, and `queue_decide` writes a `StageCacheEntry`
via `StageCache.put`. Projects are built on disk and run through the real
runner (app.runtime.runner.prepare_run / run_prepared / resume_run) — the same
pattern tests/test_run_loop_semantics.py and tests/runtime/test_hrq_cache.py
use for human_review_queue halts — so the queue snapshot these routes read is
genuine runner output, not a hand-assembled fixture. The llm_transform stage's
model call is mocked (deterministic score, no live LLM) where a test needs one.
"""
from __future__ import annotations

import json

import pandas as pd
from fastapi.testclient import TestClient

import app.runtime.runner as runner
import app.web.loading as loading
from app.main import app
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stages import llm_transform as lt
from app.services import versioning
from app.services.stage_cache import (
    HumanDecision,
    StageCache,
    StageCacheEntry,
    build_cache_id,
)
from app.services.versioning import create_version_from_disk
from app.models import RowReviewDecision

PROJECT = "queue_route_journey"


def _seed_version(root):
    vid = create_version_from_disk(root, message="test seed", reviewer="test").version_id
    versioning.publish_version(root, vid, reviewer="human")


def _write_stage(root, filename, stage):
    (root / "compiled").mkdir(parents=True, exist_ok=True)
    (root / "compiled" / filename).write_text(json.dumps(stage), encoding="utf-8")


def _load_quotes_stage(root):
    """input_data stage reading a 2-row (id, quote) csv — the MODEL INPUT the
    scoring stage judges. `review`'s own queued row does NOT carry `quote`
    (see `_score_stage`), so the only way it can appear on the page is via
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
    """human_review_queue reviewing `score`'s output; no cached decisions yet,
    so the run halts and snapshots both rows."""
    return {"id": "review", "name": "Review scores", "type": "human_review_queue",
            "inputs": [{"id": "score", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "quote", "type": "str"},
                            {"name": "score", "type": "int"}],
                "primary_key": ["id"]}}],
            "queue": {}}


def _build_and_halt(tmp_path, monkeypatch):
    """Builds load -> score (llm_transform, mocked) -> review
    (human_review_queue) and runs it for real. The run halts at `review` with
    both rows snapshotted. Returns (project_dir, run_id, run_dir, snapshot)."""
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
    return project_dir, manifest["run_id"], run_dir, snapshot


def _put_cached_decision(
    project: str, stage_id: str, run_id: str, row: pd.Series,
    decision: str, modified_score: float | None = None,
) -> None:
    """Seed a prior decision directly through the cache seam (StageCache.put)
    — never a raw store write, and never the HTTP endpoint (used by tests that
    only care about queue_page's rendering of an already-cached decision)."""
    entry = StageCacheEntry(
        id=build_cache_id(project, stage_id, row["stage_fingerprint"], row["input_fingerprint"]),
        project=project, stage_id=stage_id,
        stage_fingerprint=row["stage_fingerprint"], input_fingerprint=row["input_fingerprint"],
        source_run_id=run_id,
        frozen_input={"id": row["id"], "quote": row["quote"], "score": int(row["score"])},
        human=HumanDecision(decision=decision, modified_score=modified_score,
                             reviewer="local", reviewed_at="2026-07-01T00:00:00"),
    )
    StageCache().put(entry)


# ── 1. Happy path: snapshot + prior decisions from the cache ────────────────


def test_happy_path_renders_items_with_fingerprint_prior_decision_and_counts(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, snapshot = _build_and_halt(tmp_path, monkeypatch)
    first_fp, second_fp = snapshot["input_fingerprint"].tolist()
    first_row = snapshot[snapshot["input_fingerprint"] == first_fp].iloc[0]
    _put_cached_decision(PROJECT, "review", run_id, first_row, RowReviewDecision.approve)

    client = TestClient(app)
    r = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review")

    assert r.status_code == 200
    html = r.text
    # Both queued rows surface their input_fingerprint — the join key /decide posts against.
    assert f'data-input-fingerprint="{first_fp}"' in html
    assert f'data-input-fingerprint="{second_fp}"' in html
    # The decided row carries its prior decision; the other does not.
    assert html.count("decided-approve") == 1
    assert "<strong>approve</strong>" in html
    # reviewed_count/total: exactly one of two rows has a prior decision.
    assert "<strong>1</strong> of <strong>2</strong> reviewed" in html


# ── 2. Model-input recovery: resolvable join keys ────────────────────────────


def test_model_input_recovery_renders_the_exact_rendered_prompt(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot = _build_and_halt(tmp_path, monkeypatch)

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
    _project_dir, run_id, run_dir, snapshot = _build_and_halt(tmp_path, monkeypatch)

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    load_record = next(s for s in manifest["stages"] if s["stage_id"] == "load")
    (run_dir / load_record["output_path"]).unlink()  # the frame model_input would join against

    client = TestClient(app)
    r = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review")

    assert r.status_code == 200  # missing upstream output does not break the page
    html = r.text
    # Items still render — both fingerprints present.
    for fp in snapshot["input_fingerprint"]:
        assert f'data-input-fingerprint="{fp}"' in html
    # No rendered prompt (needs model_input) and no raw model-input dump (also
    # needs model_input): both of queue_page's model_input-gated blocks are
    # absent, evidencing model_input/rendered_prompt are None for every item.
    assert '<pre class="prompt-rendered">' not in html
    assert "model input — all fields" not in html


# ── 4. 404 on a stage that isn't a human_review_queue stage ─────────────────


def test_404_when_the_stage_id_is_not_a_human_review_queue_stage(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    r = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/load")  # `load` is input_data

    assert r.status_code == 404


# ── 5. queue_decide validation: unchanged (400s), unknown fingerprint 404s ──


def test_decide_400_on_unknown_decision(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, snapshot = _build_and_halt(tmp_path, monkeypatch)
    fp = snapshot["input_fingerprint"].iloc[0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "decision": "shrug"},
    )
    assert r.status_code == 400


def test_decide_400_when_modify_has_no_score(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, snapshot = _build_and_halt(tmp_path, monkeypatch)
    fp = snapshot["input_fingerprint"].iloc[0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "decision": "modify"},
    )
    assert r.status_code == 400


def test_decide_404_on_unknown_fingerprint_and_writes_nothing(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": "not-a-real-fingerprint", "decision": "approve"},
    )
    assert r.status_code == 404
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


# ── 6. Legacy decisions notice: counted, never applied ──────────────────────


def test_legacy_decisions_notice_counts_rows_but_does_not_apply_them(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, snapshot = _build_and_halt(tmp_path, monkeypatch)
    legacy_dir = tmp_path / PROJECT / "decisions"
    legacy_dir.mkdir(parents=True)
    pd.DataFrame([{
        "content_hash": "whatever", "decision": "approve", "modified_score": None,
        "reviewer": "local", "reviewed_at": "2026-07-01T00:00:00", "source_run_id": "run0",
    }]).to_parquet(legacy_dir / "review.parquet", index=False)

    client = TestClient(app)
    html = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    assert "1 prior decision(s) from the pre-cache format" in html
    # Not applied: both rows still render as undecided (no decided-approve class).
    assert "decided-approve" not in html
    for fp in snapshot["input_fingerprint"]:
        assert f'data-input-fingerprint="{fp}"' in html


# ── 7. End-to-end capstone: decide all three verdicts, then resume ──────────


def _e2e_load_stage(root):
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "items.csv"
    pd.DataFrame({"id": ["a", "b", "c"], "score": [1, 2, 3]}).to_csv(csv_path, index=False)
    return {"id": "load", "name": "Load items", "type": "input_data",
            "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}}}


def _e2e_review_stage():
    return {"id": "review", "name": "Review items", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
                "primary_key": ["id"]}}],
            "queue": {}}


def test_e2e_decide_approve_modify_and_reject_then_resume_completes(tmp_path, monkeypatch):
    """halt -> POST /decide for each pending row (one of each verdict) ->
    runner.resume_run -> completed manifest, with the resumed output
    reflecting each verdict: approve keeps the AI score, modify substitutes
    the human-entered score, and reject drops the row. No decisions/ directory
    is created under the project dir — every write goes through the cache."""
    project = "queue_route_e2e"
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)

    project_dir = tmp_path / project
    _write_stage(project_dir, "01_load.json", _e2e_load_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _e2e_review_stage())
    _seed_version(project_dir)

    manifest = run_prepared(prepare_run(project_dir, repo_root=project_dir))
    assert manifest["status"] == "awaiting_review"
    run_id = manifest["run_id"]

    run_dir = project_dir / "runs" / run_id
    snapshot = pd.read_parquet(run_dir / "queue" / "review.parquet")
    assert len(snapshot) == 3
    fp_by_id = dict(zip(snapshot["id"], snapshot["input_fingerprint"]))

    client = TestClient(app)
    verdicts = {
        "a": {"decision": "approve"},
        "b": {"decision": "modify", "modified_score": "99"},
        "c": {"decision": "reject"},
    }
    for row_id, form in verdicts.items():
        r = client.post(
            f"/project/{project}/runs/{run_id}/queue/review/decide",
            data={"input_fingerprint": fp_by_id[row_id], **form},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"ok": True, "input_fingerprint": fp_by_id[row_id], "decision": form["decision"]}

    # frozen_input is the upstream row the reviewer saw (id, score) alone —
    # never the fingerprint columns or the handler's decision-bookkeeping
    # placeholders (decision, modified_score, reviewer, reviewed_at), even
    # though the snapshot row `_build_cache_entry` reads from carries all of
    # those as columns.
    stage_fingerprint = snapshot["stage_fingerprint"].iloc[0]
    for row_id, fp in fp_by_id.items():
        entry = StageCacheEntry.load(build_cache_id(project, "review", stage_fingerprint, fp))
        assert set(entry.frozen_input) == {"id", "score"}

    resumed = runner.resume_run(project_dir, run_id, project_dir)
    assert resumed["status"] == "ok"

    out = pd.read_parquet(run_dir / "outputs" / "review.parquet").set_index("id")
    assert out.loc["a", "final_score"] == 1     # approve: AI score kept
    assert out.loc["b", "final_score"] == 99    # modify: human score used
    assert "c" not in out.index                 # reject: row dropped

    assert not (project_dir / "decisions").exists()
