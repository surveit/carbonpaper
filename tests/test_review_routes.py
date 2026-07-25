"""Behavior tests for the reviewer web routes (app/web/routers/review.py):
`queue_page` (GET) and `queue_decide` (POST) for one human_review_queue stage.

Both routes go through the stage-result cache (app.core.stage_cache),
never a `decisions/*.parquet` file: `queue_page`'s prior decisions come from
`StageCacheEntry.find_entries`, and `queue_decide` writes a `StageCacheEntry`
via `StageCache.record`. Projects are built on disk and run through the real
runner (app.runtime.runner.prepare_run / run_prepared / resume_run) — the same
pattern tests/test_run_loop_semantics.py and tests/runtime/test_hrq_cache.py
use for human_review_queue halts — so the queue snapshot these routes read is
genuine runner output, not a hand-assembled fixture. The llm_transform stage's
model call is mocked (deterministic score, no live LLM) where a test needs one.

The snapshot itself carries no fingerprint columns: fingerprints live in the
sidecar `<stage>.fingerprints.json` beside it, POSITIONALLY aligned to the
snapshot's row order (app.runtime.stages.human_review_queue).
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
from app.services import review, versioning
from app.core.stage_cache import StageCacheEntry
from app.services.versioning import create_version_from_disk
from app.models import RowReviewDecision, Stage

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
    so the run halts and snapshots both rows. Its output_schema declares two
    columns the queued row does NOT carry — `final_score` and `review_notes` —
    which is what makes them the reviewer's fields: the queue form renders one
    control each, and the decide route accepts exactly those names."""
    return {"id": "review", "name": "Review scores", "type": "human_review_queue",
            "inputs": [{"id": "score", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "quote", "type": "str"},
                            {"name": "score", "type": "int"}],
                "primary_key": ["id"]}}],
            "output_schema": {"columns": [
                {"name": "id", "type": "str"}, {"name": "quote", "type": "str"},
                {"name": "score", "type": "int"}, {"name": "final_score", "type": "int"},
                {"name": "review_notes", "type": "str"}, {"name": "decision", "type": "str"},
            ]},
            "queue": {}}


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
    project: str, stage_fingerprint: str, input_fingerprint: str, row: pd.Series,
    decision: RowReviewDecision, fields: dict[str, str] | None = None,
) -> None:
    """Seed a prior decision through the real review service (record_decision →
    the production cache seam) — never a hand-assembled entry, a raw store
    write, or the HTTP endpoint (used by tests that only care about
    queue_page's rendering of an already-cached decision)."""
    review.record_decision(
        project=project, stage=Stage.model_validate(_review_stage()),
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        frozen_row={"id": row["id"], "quote": row["quote"], "score": int(row["score"])},
        verdict=decision, submitted_fields=fields or {},
        reviewer="local", reviewed_at="2026-07-01T00:00:00",
    )


# ── 1. Happy path: snapshot + prior decisions from the cache ────────────────


def test_happy_path_renders_items_with_fingerprint_prior_decision_and_counts(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    first_fp, second_fp = fingerprints["input_fingerprints"]
    first_row = snapshot.iloc[0]
    _put_cached_decision(
        PROJECT, fingerprints["stage_fingerprint"], first_fp,
        first_row, RowReviewDecision.approve, {"final_score": "1"},
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


# ── 1b. The form is the stage's output_schema, not a fixed score box ─────────


def test_form_renders_one_control_per_declared_reviewer_field(tmp_path, monkeypatch):
    """`final_score` and `review_notes` are the columns _review_stage's
    output_schema declares beyond the queued row, so each gets a control —
    and `score`/`quote` (carried through) and `decision` (runtime-filled) get
    none. Nothing here is a hardcoded score box: the same page for a stage
    declaring other columns would render those instead."""
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    html = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    assert 'data-field="final_score"' in html
    assert 'data-field="review_notes"' in html
    for carried_or_filled in ("score", "quote", "id", "decision"):
        assert f'data-field="{carried_or_filled}"' not in html
    # final_score is declared `int`, so its control is a number input.
    assert '<input type="number" step="1"' in html


def test_prior_decision_prefills_the_declared_field_it_recorded(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    first_fp = fingerprints["input_fingerprints"][0]
    _put_cached_decision(
        PROJECT, fingerprints["stage_fingerprint"], first_fp, snapshot.iloc[0],
        RowReviewDecision.modify, {"final_score": "-1", "review_notes": "too generous"},
    )

    client = TestClient(app)
    html = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    assert 'value="-1" data-field="final_score"' in html
    assert 'value="too generous" data-field="review_notes"' in html
    assert "final_score → <code>-1</code>" in html


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
    load_record = next(s for s in manifest["stages"] if s["stage_id"] == "load")
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


# ── 5. queue_decide validation: FastAPI 422s malformed input, ReviewValidation-
#      Error 400s the modify-without-score domain rule, unknown fingerprint 404s ─


def test_decide_422_on_unknown_decision(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "decision": "shrug"},  # not a RowReviewDecision value
    )
    assert r.status_code == 422  # FastAPI rejects the unknown enum value
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")  # nothing written


def test_decide_400_when_a_declared_field_will_not_coerce(tmp_path, monkeypatch):
    """`final_score` is declared `int`; a non-numeric entry is refused by the
    review service against the schema — a 400, not FastAPI's 422, because only
    the stage's own declaration can say what the field accepts."""
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "decision": "modify",
              "field.final_score": "not-a-number"},
    )
    assert r.status_code == 400
    assert "not a valid int" in r.text
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")  # nothing written


def test_decide_400_on_a_field_the_output_schema_does_not_declare(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "decision": "modify", "field.invented": "7"},
    )
    assert r.status_code == 400
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


def test_decide_400_when_modify_supplies_nothing(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "decision": "modify"},
    )
    assert r.status_code == 400
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


def test_decide_409_when_the_stage_was_edited_since_the_halt(tmp_path, monkeypatch):
    """The workflow on disk declares the reviewer's fields, but the cache key
    comes from the sidecar the halt wrote. Edit the stage and the two no longer
    describe the same definition, so the decision would be orphaned — refused
    rather than written under a fingerprint nothing will look up."""
    project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]
    edited = _review_stage() | {"queue": {"reviewer_instructions": "look twice"}}
    _write_stage(project_dir, "03_review.json", edited)

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "decision": "approve"},
    )
    assert r.status_code == 409
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


def test_decide_404_on_unknown_fingerprint_and_writes_nothing(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": "not-a-real-fingerprint", "decision": "approve"},
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
            "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}}}


def _e2e_review_stage():
    """The reviewer supplies `final_score`; `score` rides through by being
    declared, and `decision` is filled by the runtime."""
    return {"id": "review", "name": "Review items", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
                "primary_key": ["id"]}}],
            "output_schema": {"columns": [
                {"name": "id", "type": "str"}, {"name": "score", "type": "int"},
                {"name": "final_score", "type": "int"}, {"name": "decision", "type": "str"},
            ]},
            "queue": {}}


def test_e2e_decide_approve_modify_and_reject_then_resume_completes(tmp_path, monkeypatch):
    """halt -> POST /decide for each pending row (one of each verdict) ->
    runner.resume_run -> completed manifest, with the resumed output
    reflecting each verdict: the declared `final_score` each row's reviewer
    supplied, and a rejected row dropped. Approve carries the reviewer's
    confirmation of the upstream score in that same declared field — the route
    has no `modified_score` parameter any more, only `field.<column>` names the
    stage's own output_schema licenses. No decisions/ directory is created
    under the project dir — every write goes through the cache."""
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
    verdicts = {
        "a": {"decision": "approve", "field.final_score": "1"},
        "b": {"decision": "modify", "field.final_score": "99"},
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
    assert list(out.columns) == ["score", "final_score", "decision"]  # exactly what was declared
    assert out.loc["a", "final_score"] == 1     # approve: the reviewer confirmed the AI score
    assert out.loc["b", "final_score"] == 99    # modify: the reviewer's own value
    assert out.loc["b", "score"] == 2           # the upstream column rode through by declaration
    assert "c" not in out.index                 # reject: row dropped

    assert not (project_dir / "decisions").exists()


def test_approve_with_no_fields_records_null_for_each_declared_field(tmp_path, monkeypatch):
    """Approve is not a synonym for "copy the AI value across": with nothing
    entered, the declared fields are recorded as null. A workflow that wants an
    upstream value downstream declares that upstream column (it rides through
    the frozen input), rather than relying on the runtime to know which column
    is "the score"."""
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data={"input_fingerprint": fp, "decision": "approve"},
    )
    assert r.status_code == 200, r.text

    entry = StageCacheEntry.read_only().get(
        PROJECT, "review", fingerprints["stage_fingerprint"], fp
    )
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["final_score"] is None
    assert entry.output_row["review_notes"] is None
    assert entry.output_row["score"] == 1        # the frozen input rode through
    assert entry.output_row["decision"] == "approve"
