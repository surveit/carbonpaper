"""Queue snapshots here are genuine runner output, not fixtures. The snapshot carries
no fingerprint columns: they live in a sidecar aligned POSITIONALLY to row order.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

import app as app_package
import app.runtime.runner as runner
import app.web.loading as loading
from app.main import app
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stages import llm_transform as lt
from app.services import review, versioning
from app.core.stage_cache import StageCacheEntry
from app.services.versioning import create_version_from_disk
from app.models import ReviewVerdict, Stage
from conftest import QUEUE_COLUMNS, queue_added_columns, queue_columns

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
                          "params": {"path": str(csv_path), "format": "csv"}},
            "output_schema": {
                "columns": [{"name": "id", "type": "str"},
                            {"name": "quote", "type": "str"}],
                "primary_key": ["id"]}}


# The reviewer columns app/services/review.py's _build_output_row (and the
# runtime's pass-through/auto-approve rows) add on top of the frozen input row.
# Every non-publish stage must declare its output_schema
# (app/models/stage.py: Stage._schemas_declared), and the runtime PROJECTS the
# stage's output onto exactly those columns.
_REVIEW_COLUMNS = queue_added_columns()


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
            "output_schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "quote", "type": "str"},
                            {"name": "score", "type": "int"}] + _REVIEW_COLUMNS,
                "primary_key": ["id"]},
            "queue": dict(QUEUE_COLUMNS)}


def _read_fingerprints(run_dir, stage_id: str = "review") -> dict:
    """The sidecar `<stage_id>.fingerprints.json` a halted queue stage writes
    beside its snapshot."""
    path = run_dir / "queue" / f"{stage_id}.fingerprints.json"
    parsed: dict = json.loads(path.read_text(encoding="utf-8"))
    return parsed


def _build_and_halt(tmp_path, monkeypatch):
    """Builds load -> score (llm_transform, mocked) -> review
    (human_review_queue) and runs it for real. The run halts at `review` with
    both rows snapshotted. Returns (project_dir, run_id, run_dir, snapshot,
    fingerprints) — fingerprints is the sidecar dict, its `input_fingerprints`
    list POSITIONALLY aligned to `snapshot`'s row order."""
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
    fingerprints = _read_fingerprints(run_dir)
    return project_dir, manifest["run_id"], run_dir, snapshot, fingerprints


def _put_cached_decision(
    project: str, stage_id: str,
    stage_fingerprint: str, input_fingerprint: str, row: pd.Series,
    decision: ReviewVerdict, reviewed_score: float | None = None,
) -> None:
    """Seed a prior decision through the real review service (record_decision →
    the production cache seam) — never a hand-assembled entry, a raw store
    write, or the HTTP endpoint (used by tests that only care about
    queue_page's rendering of an already-cached decision). `reviewed_score` is
    the reviewer's value for QUEUE_COLUMNS' one reviewed column, defaulting to
    the AI value they were shown."""
    review.record_decision(
        project=project, stage=Stage.model_validate(_review_stage()),
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        frozen_row={"id": row["id"], "quote": row["quote"], "score": int(row["score"])},
        verdict=decision,
        reviewed_values={
            "human_score": int(row["score"]) if reviewed_score is None else reviewed_score
        },
        review_notes=None,
        reviewer="local", reviewed_at="2026-07-01T00:00:00",
    )


# ── 1. Happy path: snapshot + prior decisions from the cache ────────────────


def test_happy_path_renders_items_with_fingerprint_prior_decision_and_counts(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    first_fp, second_fp = fingerprints["input_fingerprints"]
    first_row = snapshot.iloc[0]
    _put_cached_decision(
        PROJECT, "review", fingerprints["stage_fingerprint"], first_fp,
        first_row, ReviewVerdict.approve,
    )

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
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

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
    _project_dir, run_id, run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    load_record = next(s for s in manifest["stage_records"] if s["stage_id"] == "load")
    (run_dir / load_record["output_path"]).unlink()  # the frame model_input would join against

    client = TestClient(app)
    r = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review")

    assert r.status_code == 200  # missing upstream output does not break the page
    html = r.text
    # Items still render — both fingerprints present.
    for fp in fingerprints["input_fingerprints"]:
        assert f'data-input-fingerprint="{fp}"' in html
    # No rendered prompt (needs model_input) and no raw model-input dump (also
    # needs model_input): both of queue_page's model_input-gated blocks are
    # absent, evidencing model_input/rendered_prompt are None for every item.
    assert '<pre class="prompt-rendered">' not in html
    assert "model input — all fields" not in html


# ── 4. 404 on a stage that isn't a human_review_queue stage ─────────────────


def test_404_when_the_stage_id_is_not_a_human_review_queue_stage(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    r = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/load")  # `load` is input_data

    assert r.status_code == 404


# ── 5. queue_decide validation: FastAPI 422s malformed input, the endpoint and
#      the review service 400 the domain rules, unknown fingerprint 404s ───────


def test_decide_422_on_unknown_verdict(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "verdict": "shrug", "reviewer": "Ada",  # not a ReviewVerdict value
              "reviewed_values": json.dumps({"human_score": 1})},
    )
    assert r.status_code == 422  # FastAPI rejects the unknown enum value
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")  # nothing written


def test_decide_400_on_malformed_reviewed_values_json(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "verdict": "modify", "reviewer": "Ada", "reviewed_values": "{not json"},
    )
    assert r.status_code == 400
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")  # nothing written


def test_decide_400_when_reviewed_values_miss_a_declared_column(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "verdict": "modify", "reviewer": "Ada",
              "reviewed_values": json.dumps({})},
    )
    assert r.status_code == 400
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


def test_decide_404_on_unknown_fingerprint_and_writes_nothing(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": "not-a-real-fingerprint", "verdict": "approve",
              "reviewer": "Ada", "reviewed_values": json.dumps({"human_score": 1})},
    )
    assert r.status_code == 404
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


# ── 6. Snapshot pureness: exactly the upstream columns, no bookkeeping ──────


def test_snapshot_columns_are_exactly_the_upstream_columns(tmp_path, monkeypatch):
    _project_dir, _run_id, _run_dir, snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)
    assert set(snapshot.columns) == {"id", "quote", "score"}


# ── 7. End-to-end capstone: decide all three verdicts, then resume ──────────


def _e2e_load_stage(root):
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "items.csv"
    pd.DataFrame({"id": ["a", "b", "c"], "score": [1, 2, 3]}).to_csv(csv_path, index=False)
    return {"id": "load", "name": "Load items", "type": "input_data",
            "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}},
            "output_schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
                "primary_key": ["id"]}}


def _e2e_review_stage():
    return {"id": "review", "name": "Review items", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
                "primary_key": ["id"]}}],
            "output_schema": {
                "columns": [{"name": "id", "type": "str"},
                            {"name": "score", "type": "int"}] + _REVIEW_COLUMNS,
                "primary_key": ["id"]},
            "queue": dict(QUEUE_COLUMNS)}


def test_e2e_decide_every_verdict_then_resume_completes(tmp_path, monkeypatch):
    """halt -> POST /decide for each pending row -> runner.resume_run ->
    completed manifest, with the resumed output reflecting each verdict:
    approve keeps the AI score, modify substitutes the human-entered score.
    Every reviewed row is emitted. No decisions/ directory is created under the
    project dir — every write goes through the cache."""
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
    fingerprints = _read_fingerprints(run_dir)
    stage_fingerprint = fingerprints["stage_fingerprint"]
    fp_by_id = dict(zip(snapshot["id"], fingerprints["input_fingerprints"]))

    client = TestClient(app)
    ai_score_by_id = dict(zip(snapshot["id"], snapshot["score"]))
    verdicts = {
        "a": {"verdict": "approve", "reviewed_values": json.dumps(
            {"human_score": int(ai_score_by_id["a"])})},
        "b": {"verdict": "modify", "reviewed_values": json.dumps({"human_score": 99})},
        "c": {"verdict": "modify", "reviewed_values": json.dumps({"human_score": 0})},
    }
    for row_id, form in verdicts.items():
        r = client.post(
            f"/project/{project}/runs/{run_id}/queue/review/decide",
            data={"input_fingerprint": fp_by_id[row_id], "reviewer": "Ada Reviewer", **form},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"ok": True, "input_fingerprint": fp_by_id[row_id],
                        "verdict": form["verdict"]}

    # frozen_input is the upstream row the reviewer saw (id, score) alone — the
    # snapshot row the decision was recorded from carries only those columns to
    # begin with, since the snapshot is pure.
    for row_id, fp in fp_by_id.items():
        entry = StageCacheEntry.read_only().get(project, "review", stage_fingerprint, fp)
        assert entry is not None
        assert set(entry.frozen_input) == {"id", "score"}

    resumed = runner.resume_run(project_dir, run_id, project_dir)
    assert resumed["status"] == "ok"

    out = pd.read_parquet(run_dir / "outputs" / "review.parquet").set_index("id")
    assert list(out.index) == ["a", "b", "c"]   # every reviewed row is emitted
    assert out.loc["a", "human_score"] == 1     # approve: AI score kept
    assert out.loc["b", "human_score"] == 99    # modify: human score used
    assert out.loc["c", "decision"] == "modify"  # the row stays, carrying its verdict
    assert out.loc["b", "reviewer_id"] == "Ada Reviewer"  # the name the reviewer typed
    assert out.loc["c", "human_score"] == 0      # a human-entered 0 is a score, not a blank

    assert not (project_dir / "decisions").exists()


# ── 8. Attribution: the reviewer types their own name; blank is refused ──────


def test_decide_400_on_a_blank_reviewer_and_writes_nothing(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "verdict": "approve", "reviewer": "   ",
              "reviewed_values": json.dumps({"human_score": 1})},
    )
    assert r.status_code == 400
    assert "reviewer" in r.json()["detail"]
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


def test_decide_records_the_reviewer_name_the_form_posted(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "verdict": "approve", "reviewer": "  Ada Lovelace  ",
              "reviewed_values": json.dumps({"human_score": 1})},
    )
    assert r.status_code == 200, r.text

    entry = StageCacheEntry.read_only().get(
        PROJECT, "review", fingerprints["stage_fingerprint"], fp
    )
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["reviewer_id"] == "Ada Lovelace"  # trimmed, never "local"


# ── 9. skipped is the runtime's own verdict; no reviewer may post it ─────────


def test_decide_400_on_verdict_skipped(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "verdict": "skipped", "reviewer": "Ada",
              "reviewed_values": json.dumps({"human_score": 1})},
    )
    assert r.status_code == 400
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


# ── 10. Coercion against the declared column ────────────────────────────────


def test_decide_400_on_a_value_that_will_not_coerce(tmp_path, monkeypatch):
    """`human_score` reviews an `int` source column, so free text is refused
    naming the column and the offending text — never stored as a string."""
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "verdict": "modify", "reviewer": "Ada",
              "reviewed_values": json.dumps({"human_score": "banana"})},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "human_score" in detail and "banana" in detail
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


def test_decide_coerces_form_text_to_the_declared_type(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "verdict": "modify", "reviewer": "Ada",
              "reviewed_values": json.dumps({"human_score": "  -3 "})},
    )
    assert r.status_code == 200, r.text

    entry = StageCacheEntry.read_only().get(
        PROJECT, "review", fingerprints["stage_fingerprint"], fp
    )
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["human_score"] == -3
    assert not isinstance(entry.output_row["human_score"], str)


# ── 11. Notes: normalised at the boundary, refused with no notes column ──────


def test_decide_accepts_an_untouched_notes_box_as_no_note(tmp_path, monkeypatch):
    """An HTML form posts an empty textarea as "" — that is no note, not an
    empty note, and must not reach the notes column as one."""
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "verdict": "approve", "reviewer": "Ada",
              "reviewed_values": json.dumps({"human_score": 1}), "review_notes": "   "},
    )
    assert r.status_code == 200, r.text

    entry = StageCacheEntry.read_only().get(
        PROJECT, "review", fingerprints["stage_fingerprint"], fp
    )
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["review_notes"] is None


def _no_notes_review_stage():
    queue = {k: v for k, v in QUEUE_COLUMNS.items() if k != "review_notes_column"}
    return {"id": "review", "name": "Review items", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
                "primary_key": ["id"]}}],
            "queue": queue}


def test_decide_400_on_notes_when_the_stage_declares_no_notes_column(tmp_path, monkeypatch):
    project = "queue_route_no_notes"
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    project_dir = tmp_path / project
    _write_stage(project_dir, "01_load.json", _e2e_load_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _no_notes_review_stage())
    _seed_version(project_dir)

    manifest = run_prepared(prepare_run(project_dir, repo_root=project_dir))
    run_id = manifest["run_id"]
    fingerprints = _read_fingerprints(project_dir / "runs" / run_id)

    client = TestClient(app)
    r = client.post(
        f"/project/{project}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fingerprints["input_fingerprints"][0],
              "verdict": "modify", "reviewer": "Ada",
              "reviewed_values": json.dumps({"human_score": 2}),
              "review_notes": "a note nobody declared a home for"},
    )
    assert r.status_code == 400
    assert not StageCacheEntry.list(prefix=f"{project}/review/")


# ── 12. Live-definition drift from the halted run ────────────────────────────


def _drift_the_review_stage(project_dir):
    """Rename the reviewed target on the LIVE definition after the run halted, so
    the live stage fingerprint no longer matches the sidecar's."""
    drifted = _review_stage()
    drifted["queue"] = {**QUEUE_COLUMNS, "reviewed_columns": {"score": "checked_score"}}
    _write_stage(project_dir, "03_review.json", drifted)


def test_queue_page_states_the_drift_and_renders_no_items(tmp_path, monkeypatch):
    """The run's decisions are keyed by the fingerprint it halted under, but the
    columns they are read and written through come from the live definition. Once
    the two describe different column sets the page says so in place of the rows,
    rather than raising KeyError or half-rendering them."""
    project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)
    _drift_the_review_stage(project_dir)

    r = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review")

    assert r.status_code == 200
    assert "has changed since this run halted" in r.text
    assert 'class="definition-drift"' in r.text
    assert "data-input-fingerprint" not in r.text  # no row contents at all


def test_decide_409_when_the_stage_changed_since_the_halt(tmp_path, monkeypatch):
    project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    _drift_the_review_stage(project_dir)

    r = TestClient(app).post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fingerprints["input_fingerprints"][0],
              "verdict": "approve", "reviewer": "Ada",
              "reviewed_values": json.dumps({"checked_score": 1})},
    )
    assert r.status_code == 409
    assert "has changed since this run halted" in r.json()["detail"]
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


# ── 13. The form the page renders: one field per declared reviewed column ────


def test_queue_page_renders_one_prefilled_field_per_reviewed_column(tmp_path, monkeypatch):
    """The field is generated from queue.reviewed_columns and typed from the
    declared column — a number input for an `int`, pre-filled with the AI value
    the reviewer is being asked to confirm or change."""
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    html = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    assert 'data-target="human_score"' in html
    assert 'type="number"' in html
    assert 'data-ai-value="1"' in html and 'value="1"' in html  # the mocked AI score
    assert "modified_score" not in html  # the hardcoded -2..2 score field is gone
    assert 'data-action="reject"' not in html


def test_queue_page_gates_the_items_behind_the_reviewer_name(tmp_path, monkeypatch):
    """The gate takes BOTH halves: the container carries `hidden`, and the
    stylesheet answers it with an [hidden] rule. Without the second half the
    container's own `display: flex` beats the UA rule and the rows render anyway,
    so asserting the markup alone passes while the gate is visually inert."""
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    assert 'id="reviewer-name"' in html
    container = re.search(r"<div[^>]*id=\"queue-items\"[^>]*>", html)
    assert container is not None and re.search(r"\bhidden\b", container.group(0))

    stylesheet = (Path(app_package.__file__).parent / "static" / "style.css").read_text(
        encoding="utf-8"
    )
    assert re.search(r"\.queue-items\[hidden\]\s*\{[^}]*display:\s*none", stylesheet)


def test_queue_page_prefills_a_decided_row_from_the_recorded_value(tmp_path, monkeypatch):
    """A decided row opens with what the reviewer recorded, not the AI value it
    contradicts — otherwise Approve/Save silently reverts their own decision. The
    AI value stays visible beside it, labelled separately."""
    _project_dir, run_id, _run_dir, snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    first_fp = fingerprints["input_fingerprints"][0]
    _put_cached_decision(
        PROJECT, "review", fingerprints["stage_fingerprint"], first_fp,
        snapshot.iloc[0], ReviewVerdict.modify, reviewed_score=99,
    )

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    decided = html[html.index(f'data-input-fingerprint="{first_fp}"'):]
    decided = decided[:decided.index("</article>")]
    assert 'value="99"' in decided          # the field opens on the recorded value
    assert 'data-ai-value="1"' in decided   # Approve still posts the AI value
    assert "you recorded" in decided


# ── 14. A nullable bool: three states, so never a fabricated `false` ─────────


def _bool_review_stage():
    return {"id": "review", "name": "Review flags", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str"},
                            {"name": "flag", "type": "bool", "nullable": True}],
                "primary_key": ["id"]}}],
            "queue": {**queue_columns(source="flag", target="human_flag")}}


def _build_and_halt_bool_queue(tmp_path, monkeypatch):
    """A queue over a nullable `bool` column whose AI value is null on every row."""
    project = "queue_route_bool"
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    project_dir = tmp_path / project
    (project_dir / "data").mkdir(parents=True, exist_ok=True)
    csv_path = project_dir / "data" / "flags.csv"
    pd.DataFrame({"id": ["a"], "flag": [None]}).to_csv(csv_path, index=False)
    _write_stage(project_dir, "01_load.json", {
        "id": "load", "name": "Load flags", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}}})
    _write_stage(project_dir, "02_review.json", _bool_review_stage())
    _seed_version(project_dir)
    manifest = run_prepared(prepare_run(project_dir, repo_root=project_dir))
    return project, manifest["run_id"]


def test_a_null_bool_ai_value_is_never_rendered_as_false(tmp_path, monkeypatch):
    """A checkbox has two states and a nullable bool has three, so a checkbox
    would advertise a missing AI value as `false` and Approve would post it. The
    field is a select whose unset option is what a null renders as."""
    project, run_id = _build_and_halt_bool_queue(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    assert 'type="checkbox"' not in html
    assert 'data-ai-value=""' in html      # the null AI value, not "false"
    assert "— unset —" in html
    assert 'data-target="human_flag"' in html
    assert "selected" not in html          # nothing pre-selected: there is no value


# ── 15. Reviewed-value key handling at the endpoint ─────────────────────────


def test_decide_400_on_reviewed_values_that_is_not_a_json_object(tmp_path, monkeypatch):
    """`reviewed_values` is keyed by target column; a JSON array names nothing."""
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)

    r = TestClient(app).post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fingerprints["input_fingerprints"][0],
              "verdict": "modify", "reviewer": "Ada", "reviewed_values": "[1, 2]"},
    )
    assert r.status_code == 400
    assert "JSON object" in r.json()["detail"]
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


def test_decide_400_on_an_undeclared_reviewed_value_key(tmp_path, monkeypatch):
    """The seam `_coerce_reviewed_values` leaves open: an undeclared key has no
    column to coerce against, so it passes through uncoerced and the review
    service — the sole authority on the key set — refuses the whole decision."""
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)

    r = TestClient(app).post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fingerprints["input_fingerprints"][0],
              "verdict": "modify", "reviewer": "Ada",
              "reviewed_values": json.dumps({"human_score": 1, "smuggled": "x"})},
    )
    assert r.status_code == 400
    assert "smuggled" in r.json()["detail"]
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


# ── 16. The output_schema path of the declared-column lookup ────────────────


def _output_schema_review_stage():
    """Declares an output_schema, so `human_score` resolves from THERE rather than
    from the input edge's `score`. The two differ on the one spec field the model
    lets them differ on: `score` is non-nullable, `human_score` is nullable. That
    is the evidence of which declaration the endpoint coerced against — a blank
    value is a null through the output_schema column and a refusal through the
    source column."""
    return {"id": "review", "name": "Review items", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str"},
                            {"name": "score", "type": "int", "nullable": False,
                             "range": [0, 5]}],
                "primary_key": ["id"]}}],
            "output_schema": {"columns": [
                {"name": "id", "type": "str"},
                {"name": "score", "type": "int", "nullable": False, "range": [0, 5]},
                {"name": "human_score", "type": "int", "nullable": True, "range": [0, 5]},
                {"name": "decision", "type": "str"}, {"name": "reviewer_id", "type": "str"},
                {"name": "reviewed_at", "type": "str"}, {"name": "review_notes", "type": "str"}]},
            "queue": dict(QUEUE_COLUMNS)}


def _build_and_halt_output_schema_queue(tmp_path, monkeypatch, project):
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    project_dir = tmp_path / project
    _write_stage(project_dir, "01_load.json", _e2e_load_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _output_schema_review_stage())
    _seed_version(project_dir)
    run_id = run_prepared(prepare_run(project_dir, repo_root=project_dir))["run_id"]
    return run_id, _read_fingerprints(project_dir / "runs" / run_id)


def test_decide_coerces_against_the_output_schema_column_when_declared(tmp_path, monkeypatch):
    project = "queue_route_output_schema"
    run_id, fingerprints = _build_and_halt_output_schema_queue(tmp_path, monkeypatch, project)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    url = f"/project/{project}/runs/{run_id}/queue/review/decide"
    refused = client.post(url, data={
        "input_fingerprint": fp, "verdict": "modify", "reviewer": "Ada",
        "reviewed_values": json.dumps({"human_score": 9})})
    assert refused.status_code == 400  # outside the declared [0, 5]
    assert "above the declared maximum" in refused.json()["detail"]

    # Blank is a null only because output_schema's `human_score` is nullable; the
    # input edge's `score` is not, so this is the output_schema path being read.
    accepted = client.post(url, data={
        "input_fingerprint": fp, "verdict": "modify", "reviewer": "Ada",
        "reviewed_values": json.dumps({"human_score": ""})})
    assert accepted.status_code == 200, accepted.text
    entry = StageCacheEntry.read_only().get(
        project, "review", fingerprints["stage_fingerprint"], fp)
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["human_score"] is None


def test_queue_page_renders_the_declared_range_on_the_field(tmp_path, monkeypatch):
    project = "queue_route_output_schema_page"
    run_id, _fingerprints = _build_and_halt_output_schema_queue(tmp_path, monkeypatch, project)

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    assert 'min="0"' in html and 'max="5"' in html
