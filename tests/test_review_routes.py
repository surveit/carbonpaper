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
import app.web.routers.review as review_routes
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
    """input_data stage reading a 2-row (id, quote) csv."""
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
    survives onto the queued row."""
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


def _find_stage_def(project: str, stage_id: str) -> Stage:
    stage_def = loading.find_stage(loading.load_stages(project).stages, stage_id)
    assert stage_def is not None
    return stage_def


def _decide_data(fp, reviewed, prefilled=None, reviewer="Ada", **extra):
    """The form a browser posts to /decide. `prefilled` defaults to `reviewed`,
    the unchanged submit the endpoint derives `approve` from; pass a different
    mapping to post a changed value. Either may be a raw string, for the
    malformed-payload cases."""
    if prefilled is None:
        prefilled = {} if isinstance(reviewed, str) else reviewed
    return {
        "input_fingerprint": fp, "reviewer": reviewer,
        "reviewed_values": reviewed if isinstance(reviewed, str) else json.dumps(reviewed),
        "prefilled_values": prefilled if isinstance(prefilled, str) else json.dumps(prefilled),
        **extra,
    }


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


# ── 2. Lineage: the sidecar's ordinal against the declared upstream stage ────


def test_lineage_urls_name_the_upstream_stage_and_the_sidecar_ordinal(tmp_path, monkeypatch):
    """The queue stage has produced no output at halt time, so a row's lineage
    link names the UPSTREAM stage (`score`) and the row's ordinal from the
    sidecar — never the queue stage itself, and never a guessed position."""
    _project_dir, run_id, _run_dir, _snapshot, sidecar = _build_and_halt(tmp_path, monkeypatch)
    assert sidecar["row_ordinals"] == [0, 1]

    stage_def = _find_stage_def(PROJECT, "review")
    fingerprints = loading.load_queue_fingerprints(PROJECT, run_id, "review")
    urls = review_routes._build_lineage_urls(
        PROJECT, run_id, review_routes._resolve_lineage(stage_def, fingerprints), fingerprints
    )

    assert urls == [
        f"/project/{PROJECT}/runs/{run_id}/stage/score/row/0/trace/view",
        f"/project/{PROJECT}/runs/{run_id}/stage/score/row/1/trace/view",
    ]


def test_a_sidecar_without_row_ordinals_yields_no_lineage_link(tmp_path, monkeypatch):
    """A run halted before the runtime recorded ordinals has no exact row to
    link to, so the page states that instead of linking a guessed position."""
    _project_dir, run_id, run_dir, _snapshot, _sidecar = _build_and_halt(tmp_path, monkeypatch)
    path = run_dir / "queue" / "review.fingerprints.json"
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    del sidecar["row_ordinals"]
    path.write_text(json.dumps(sidecar), encoding="utf-8")

    stage_def = _find_stage_def(PROJECT, "review")
    fingerprints = loading.load_queue_fingerprints(PROJECT, run_id, "review")
    lineage = review_routes._resolve_lineage(stage_def, fingerprints)

    assert lineage.upstream_stage_id is None
    assert "ordinal" in (lineage.note or "")
    assert review_routes._build_lineage_urls(PROJECT, run_id, lineage, fingerprints) == [None, None]


# ── 4. 404 on a stage that isn't a human_review_queue stage ─────────────────


def test_404_when_the_stage_id_is_not_a_human_review_queue_stage(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    r = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/load")  # `load` is input_data

    assert r.status_code == 404


# ── 5. queue_decide validation: the endpoint and the review service 400 the
#      domain rules, unknown fingerprint 404s ────────────────────────────────


def test_decide_400_on_malformed_reviewed_values_json(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fp, "{not json"),
    )
    assert r.status_code == 400
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")  # nothing written


def test_decide_400_when_the_two_value_maps_name_different_columns(tmp_path, monkeypatch):
    """The verdict is derived by comparing them column by column, so a column
    present in only one is a comparison that cannot be made — never one
    silently treated as unchanged."""
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    r = TestClient(app).post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fp, {"human_score": 1}, prefilled={}),
    )
    assert r.status_code == 400
    assert "human_score" in r.json()["detail"]
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


def test_decide_400_when_reviewed_values_miss_a_declared_column(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fp, {}),
    )
    assert r.status_code == 400
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


def test_decide_404_on_unknown_fingerprint_and_writes_nothing(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data("not-a-real-fingerprint", {"human_score": 1}),
    )
    assert r.status_code == 404
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


# ── 5b. The verdict is DERIVED from submitted vs pre-filled values ───────────


def test_decide_derives_approve_when_every_value_matches_the_prefill(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    r = TestClient(app).post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fp, {"human_score": "1"}, prefilled={"human_score": "1"}),
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "approve"

    entry = StageCacheEntry.read_only().get(
        PROJECT, "review", fingerprints["stage_fingerprint"], fp)
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["decision"] == "approve"


def test_decide_derives_modify_when_a_value_differs_from_the_prefill(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    r = TestClient(app).post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fp, {"human_score": "4"}, prefilled={"human_score": "1"}),
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "modify"

    entry = StageCacheEntry.read_only().get(
        PROJECT, "review", fingerprints["stage_fingerprint"], fp)
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["decision"] == "modify"
    assert entry.output_row["human_score"] == 4


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
    completed manifest, with the resumed output reflecting each DERIVED verdict:
    a submit matching the prefill records approve and keeps the AI score, one
    differing from it records modify and substitutes the human-entered score.
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
    # Each row's prefill is the AI value the page opened on; submitting it
    # unchanged derives approve, submitting anything else derives modify.
    ai_score_by_id = {k: str(v) for k, v in zip(snapshot["id"], snapshot["score"])}
    submitted = {"a": ai_score_by_id["a"], "b": "99", "c": "0"}
    expected_verdict = {"a": "approve", "b": "modify", "c": "modify"}
    for row_id, value in submitted.items():
        r = client.post(
            f"/project/{project}/runs/{run_id}/queue/review/decide",
            data=_decide_data(
                fp_by_id[row_id], {"human_score": value},
                prefilled={"human_score": ai_score_by_id[row_id]},
                reviewer="Ada Reviewer",
            ),
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "input_fingerprint": fp_by_id[row_id],
                            "verdict": expected_verdict[row_id]}

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
        data=_decide_data(fp, {"human_score": 1}, reviewer="   "),
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
        data=_decide_data(fp, {"human_score": 1}, reviewer="  Ada Lovelace  "),
    )
    assert r.status_code == 200, r.text

    entry = StageCacheEntry.read_only().get(
        PROJECT, "review", fingerprints["stage_fingerprint"], fp
    )
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["reviewer_id"] == "Ada Lovelace"  # trimmed, never "local"


# ── 9. The reviewer names no verdict — a posted one is not a decision ────────


def test_decide_ignores_a_posted_verdict_and_records_the_derived_one(tmp_path, monkeypatch):
    """The verdict comes from what changed, so a `verdict` field on the form is
    inert — `skipped` (the runtime's own verdict, which the review service
    refuses: tests/services/test_review.py) cannot be smuggled in through it."""
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    r = TestClient(app).post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fp, {"human_score": 1}, verdict="skipped"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "approve"


# ── 10. Coercion against the declared column ────────────────────────────────


def test_decide_400_on_a_value_that_will_not_coerce(tmp_path, monkeypatch):
    """`human_score` reviews an `int` source column, so free text is refused
    naming the column and the offending text — never stored as a string."""
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fp, {"human_score": "banana"}, prefilled={"human_score": "1"}),
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
        data=_decide_data(fp, {"human_score": "  -3 "}, prefilled={"human_score": "1"}),
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
        data=_decide_data(fp, {"human_score": 1}, review_notes="   "),
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
        data=_decide_data(
            fingerprints["input_fingerprints"][0], {"human_score": 2},
            prefilled={"human_score": 1},
            review_notes="a note nobody declared a home for",
        ),
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
        data=_decide_data(fingerprints["input_fingerprints"][0], {"checked_score": 1}),
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
    assert 'data-prefill="1"' in html and 'value="1"' in html  # the mocked AI score
    assert "modified_score" not in html  # the hardcoded -2..2 score field is gone
    assert 'data-action="reject"' not in html
    # One Submit; the verdict is derived, so no button names one.
    assert 'data-action="approve"' not in html
    assert html.count(">Submit<") == html.count("<article class=\"queue-card")


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
    """A decided row opens with what the reviewer recorded, not the value it
    contradicts — otherwise an untouched submit silently reverts their own
    decision. What the stage received stays visible beside it, labelled
    separately."""
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
    # what the stage received stays visible beside it, labelled as received
    assert "received <code>score</code>: <strong>1</strong>" in " ".join(decided.split())
    assert "you recorded" in decided


# ── 14. A nullable bool: three states, so never a fabricated `false` ─────────


def _bool_review_stage(nullable):
    return {"id": "review", "name": "Review flags", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str"},
                            {"name": "flag", "type": "bool", "nullable": nullable}],
                "primary_key": ["id"]}}],
            "queue": {**queue_columns(source="flag", target="human_flag")}}


def _build_and_halt_bool_queue(tmp_path, monkeypatch, project, *, ai_value, nullable=True):
    """A one-row queue over a `bool` column whose AI value is `ai_value` (None
    for a null). Returns (project, run_id, fingerprints, snapshot)."""
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    project_dir = tmp_path / project
    (project_dir / "data").mkdir(parents=True, exist_ok=True)
    csv_path = project_dir / "data" / "flags.csv"
    pd.DataFrame({"id": ["a"], "flag": [ai_value]}).to_csv(csv_path, index=False)
    _write_stage(project_dir, "01_load.json", {
        "id": "load", "name": "Load flags", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}}})
    _write_stage(project_dir, "02_review.json", _bool_review_stage(nullable))
    _seed_version(project_dir)
    run_id = run_prepared(prepare_run(project_dir, repo_root=project_dir))["run_id"]
    run_dir = project_dir / "runs" / run_id
    return run_id, _read_fingerprints(run_dir), pd.read_parquet(run_dir / "queue" / "review.parquet")


def _find_selected_option(html, target):
    """The value of the `selected` option of `target`'s select, or None when the
    select pre-selects nothing — in which case a browser falls back to whichever
    option happens to be FIRST, so "nothing selected" is never a safe state."""
    select = re.search(
        rf'<select[^>]*data-target="{target}"[^>]*>(.*?)</select>', html, re.DOTALL
    )
    assert select is not None, f"no select rendered for {target!r}"
    chosen = re.search(r'<option value="([^"]*)"[^>]*\bselected\b', select.group(1))
    return None if chosen is None else chosen.group(1)


def test_a_null_bool_ai_value_is_never_rendered_as_false(tmp_path, monkeypatch):
    """A checkbox has two states and a nullable bool has three, so a checkbox
    would advertise a missing AI value as `false` and Approve would post it. The
    field is a select that opens EXPLICITLY on its unset option — not merely
    without a selection, which would leave the browser showing `true`."""
    run_id, _fingerprints, _snapshot = _build_and_halt_bool_queue(
        tmp_path, monkeypatch, "queue_route_bool_null", ai_value=None)

    html = TestClient(app).get(f"/project/queue_route_bool_null/runs/{run_id}/queue/review").text

    assert 'type="checkbox"' not in html
    # the null upstream value shown as null, not as "false"
    assert "received <code>flag</code>: <strong><em>null</em></strong>" in " ".join(html.split())
    assert "— unset —" in html
    assert _find_selected_option(html, "human_flag") == ""


def test_a_bool_select_opens_on_the_recorded_value_of_a_decided_row(tmp_path, monkeypatch):
    """The recorded `true` must come back SELECTED. Left unselected, the unset
    option renders first, the browser shows it, and an untouched Save posts ""
    — silently reverting the reviewer's own decision."""
    project = "queue_route_bool_recorded"
    run_id, fingerprints, snapshot = _build_and_halt_bool_queue(
        tmp_path, monkeypatch, project, ai_value=False)
    review.record_decision(
        project=project, stage=Stage.model_validate(_bool_review_stage(True)),
        stage_fingerprint=fingerprints["stage_fingerprint"],
        input_fingerprint=fingerprints["input_fingerprints"][0],
        frozen_row={"id": snapshot.iloc[0]["id"], "flag": bool(snapshot.iloc[0]["flag"])},
        verdict=ReviewVerdict.modify, reviewed_values={"human_flag": True},
        review_notes=None, reviewer="Ada", reviewed_at="2026-07-01T00:00:00",
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    assert _find_selected_option(html, "human_flag") == "true"
    # what the stage received stays visible beside the recorded value
    assert "received <code>flag</code>: <strong>false</strong>" in " ".join(html.split())
    # The labels spell the value the way the options do — never a python repr
    # sitting beside a select that reads `true`.
    assert "you recorded <strong>true</strong>" in " ".join(html.split())
    assert "True" not in html and "False" not in html


def test_a_non_nullable_bool_select_opens_on_the_ai_value(tmp_path, monkeypatch):
    """With no unset option to fall back on, an unselected select shows whichever
    option is FIRST — `true` — so a row the model said `false` for would record
    `true` on an untouched Save. The AI value must be selected."""
    project = "queue_route_bool_required"
    run_id, fingerprints, _snapshot = _build_and_halt_bool_queue(
        tmp_path, monkeypatch, project, ai_value=False, nullable=False)

    client = TestClient(app)
    html = client.get(f"/project/{project}/runs/{run_id}/queue/review").text
    assert "— unset —" not in html                              # nothing to be unset to
    assert _find_selected_option(html, "human_flag") == "false"

    # Submitting what the page opened on records `false`, not the first option
    # — and, unchanged from the prefill, derives `approve`.
    r = client.post(
        f"/project/{project}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fingerprints["input_fingerprints"][0], {"human_flag": "false"}),
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "approve"
    entry = StageCacheEntry.read_only().get(
        project, "review", fingerprints["stage_fingerprint"],
        fingerprints["input_fingerprints"][0])
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["human_flag"] is False


def test_an_enum_select_opens_on_the_recorded_value(tmp_path, monkeypatch):
    """The enum path goes through the same option comparison as bool."""
    project = "queue_route_enum"
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    project_dir = tmp_path / project
    (project_dir / "data").mkdir(parents=True, exist_ok=True)
    csv_path = project_dir / "data" / "calls.csv"
    pd.DataFrame({"id": ["a"], "call": ["no"]}).to_csv(csv_path, index=False)
    stage = {"id": "review", "name": "Review calls", "type": "human_review_queue",
             "inputs": [{"id": "load", "schema": {
                 "columns": [{"name": "id", "type": "str"},
                             {"name": "call", "type": "str", "nullable": False,
                              "enum": ["yes", "no", "unclear"]}],
                 "primary_key": ["id"]}}],
             "queue": {**queue_columns(source="call", target="human_call")}}
    _write_stage(project_dir, "01_load.json", {
        "id": "load", "name": "Load calls", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}}})
    _write_stage(project_dir, "02_review.json", stage)
    _seed_version(project_dir)
    run_id = run_prepared(prepare_run(project_dir, repo_root=project_dir))["run_id"]
    fingerprints = _read_fingerprints(project_dir / "runs" / run_id)
    review.record_decision(
        project=project, stage=Stage.model_validate(stage),
        stage_fingerprint=fingerprints["stage_fingerprint"],
        input_fingerprint=fingerprints["input_fingerprints"][0],
        frozen_row={"id": "a", "call": "no"},
        verdict=ReviewVerdict.modify, reviewed_values={"human_call": "unclear"},
        review_notes=None, reviewer="Ada", reviewed_at="2026-07-01T00:00:00",
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    assert _find_selected_option(html, "human_call") == "unclear"


# ── 14b. date/datetime controls: ISO 8601 or the control renders blank ──────


def _temporal_review_stage(column_type):
    return {"id": "review", "name": "Review times", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str"},
                            {"name": "seen_at", "type": column_type}],
                "primary_key": ["id"]}}],
            "queue": {**queue_columns(source="seen_at", target="human_seen_at")}}


def _decide_a_temporal_row(tmp_path, monkeypatch, project, column_type, recorded):
    """Runs a one-row queue over a `date`/`datetime` column, records `recorded`
    for it through the real endpoint, and returns the reloaded page."""
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    project_dir = tmp_path / project
    (project_dir / "data").mkdir(parents=True, exist_ok=True)
    csv_path = project_dir / "data" / "sightings.csv"
    pd.DataFrame({"id": ["a"], "seen_at": ["2026-01-01T08:00:00"]}).to_csv(csv_path, index=False)
    _write_stage(project_dir, "01_load.json", {
        "id": "load", "name": "Load sightings", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}}})
    _write_stage(project_dir, "02_review.json", _temporal_review_stage(column_type))
    _seed_version(project_dir)
    run_id = run_prepared(prepare_run(project_dir, repo_root=project_dir))["run_id"]
    fingerprints = _read_fingerprints(project_dir / "runs" / run_id)

    client = TestClient(app)
    r = client.post(
        f"/project/{project}/runs/{run_id}/queue/review/decide",
        data=_decide_data(
            fingerprints["input_fingerprints"][0], {"human_seen_at": recorded},
            prefilled={"human_seen_at": "2026-01-01T08:00:00"},
        ),
    )
    assert r.status_code == 200, r.text
    return client.get(f"/project/{project}/runs/{run_id}/queue/review").text


def _find_input_value(html, target):
    field = re.search(rf'<input[^>]*data-target="{target}"[^>]*>', html, re.DOTALL)
    assert field is not None, f"no input rendered for {target!r}"
    # `\bvalue=` would match inside `data-prefill=`, which is a different value.
    value = re.search(r'\svalue="([^"]*)"', field.group(0))
    return None if value is None else value.group(1)


def test_a_datetime_control_opens_on_the_recorded_value_of_a_decided_row(tmp_path, monkeypatch):
    """The recorded value comes back from the cache stringified — space-
    separated, which a `datetime-local` control rejects, rendering BLANK on a row
    that has a value. An untouched Save would then post "" over it."""
    html = _decide_a_temporal_row(
        tmp_path, monkeypatch, "queue_route_datetime", "datetime", "2026-03-04T09:30:00")

    assert 'type="datetime-local"' in html
    assert _find_input_value(html, "human_seen_at") == "2026-03-04T09:30:00"


def test_a_date_control_opens_on_the_recorded_value_of_a_decided_row(tmp_path, monkeypatch):
    html = _decide_a_temporal_row(
        tmp_path, monkeypatch, "queue_route_date", "date", "2026-03-04")

    assert 'type="date"' in html
    assert _find_input_value(html, "human_seen_at") == "2026-03-04"


# ── 15. Reviewed-value key handling at the endpoint ─────────────────────────


def test_decide_400_on_reviewed_values_that_is_not_a_json_object(tmp_path, monkeypatch):
    """`reviewed_values` is keyed by target column; a JSON array names nothing."""
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)

    r = TestClient(app).post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fingerprints["input_fingerprints"][0], "[1, 2]"),
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
        data=_decide_data(
            fingerprints["input_fingerprints"][0], {"human_score": 1, "smuggled": "x"}),
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
    refused = client.post(url, data=_decide_data(
        fp, {"human_score": 9}, prefilled={"human_score": 1}))
    assert refused.status_code == 400  # outside the declared [0, 5]
    assert "above the declared maximum" in refused.json()["detail"]

    # Blank is a null only because output_schema's `human_score` is nullable; the
    # input edge's `score` is not, so this is the output_schema path being read.
    accepted = client.post(url, data=_decide_data(
        fp, {"human_score": ""}, prefilled={"human_score": 1}))
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


# ── 17. The queued rows are described from the DECLARED input schema ─────────
#
# `human_review_queue` runs for any workflow, so nothing here may depend on the
# upstream stage's type or on any particular column name.


def _build_and_halt_queue_over(tmp_path, monkeypatch, project, stages):
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    project_dir = tmp_path / project
    for index, stage in enumerate(stages, start=1):
        _write_stage(project_dir, f"{index:02d}_{stage['id']}.json", stage)
    _seed_version(project_dir)
    manifest = run_prepared(prepare_run(project_dir, repo_root=project_dir))
    assert manifest["status"] == "awaiting_review", manifest
    run_id = manifest["run_id"]
    return run_id, _read_fingerprints(project_dir / "runs" / run_id)


def _lineage_urls(project, run_id, stage_id="review"):
    stage_def = _find_stage_def(project, stage_id)
    fingerprints = loading.load_queue_fingerprints(project, run_id, stage_id)
    assert fingerprints is not None
    return review_routes._build_lineage_urls(
        project, run_id, review_routes._resolve_lineage(stage_def, fingerprints), fingerprints
    )


def test_a_queue_directly_on_input_data_renders_and_links_to_that_stage(tmp_path, monkeypatch):
    """The snapshot IS the material to review here — there is no model between
    the input and the queue — so the page renders in full and traces the
    `input_data` stage's row."""
    project = "queue_route_on_input_data"
    project_dir = tmp_path / project
    run_id, fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _e2e_review_stage()],
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text
    for fp in fingerprints["input_fingerprints"]:
        assert f'data-input-fingerprint="{fp}"' in html
    assert "reviewing blind" not in html

    assert _lineage_urls(project, run_id) == [
        f"/project/{project}/runs/{run_id}/stage/load/row/{o}/trace/view"
        for o in fingerprints["row_ordinals"]
    ]


def _labelled_row_function_stage():
    """python_row_function upstream: no model produced these values, and the
    queue's input edge declares a description for the column it adds."""
    code = (
        "def transform(row):\n"
        "    return {'id': row['id'], 'score': row['score'],\n"
        "            'label': 'high' if row['score'] > 1 else 'low'}"
    )
    return {"id": "label", "name": "Label items", "type": "python_row_function",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
                "primary_key": ["id"]}}],
            "function": {"kind": "inline", "code": code},
            "output_schema": {"columns": [
                {"name": "id", "type": "str"}, {"name": "score", "type": "int"},
                {"name": "label", "type": "str"}]}}


def _review_labels_stage():
    return {"id": "review", "name": "Review labels", "type": "human_review_queue",
            "inputs": [{"id": "label", "schema": {
                "columns": [
                    {"name": "id", "type": "str"},
                    {"name": "score", "type": "int"},
                    {"name": "label", "type": "str",
                     "description": "high when the score exceeds one"}],
                "primary_key": ["id"]}}],
            "queue": {**queue_columns(source="label", target="human_label")}}


def test_a_queue_whose_upstream_is_not_an_llm_transform_renders_and_links(tmp_path, monkeypatch):
    """A `python_row_function` computed these values, so nothing on the page may
    attribute them to a model: the queue stage cannot know what produced what it
    received, and telling the reviewer "AI" is exactly the fabrication this
    surface was rewritten to remove."""
    project = "queue_route_on_row_function"
    project_dir = tmp_path / project
    run_id, fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _labelled_row_function_stage(), _review_labels_stage()],
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text
    for fp in fingerprints["input_fingerprints"]:
        assert f'data-input-fingerprint="{fp}"' in html
    assert 'data-target="human_label"' in html
    assert "AI" not in html
    assert "received <code>label</code>:" in " ".join(html.split())

    assert _lineage_urls(project, run_id) == [
        f"/project/{project}/runs/{run_id}/stage/label/row/{o}/trace/view"
        for o in fingerprints["row_ordinals"]
    ]


def test_queued_columns_carry_the_declared_description_and_primary_key(tmp_path, monkeypatch):
    project = "queue_route_column_metadata"
    project_dir = tmp_path / project
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _labelled_row_function_stage(), _review_labels_stage()],
    )

    described = review_routes._describe_queued_columns(
        _find_stage_def(project, "review"),
        loading.queue_snapshot(project, run_id, "review"),
    )

    by_name = {column.name: column for column in described.columns}
    assert by_name["label"].description == "high when the score exceeds one"
    assert not by_name["label"].in_primary_key
    assert by_name["id"].in_primary_key and by_name["id"].description is None
    assert described.schema_note is None and described.identity_note is None


def _no_primary_key_review_stage():
    return {"id": "review", "name": "Review items", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str"},
                            {"name": "score", "type": "int"}]}}],
            "queue": dict(QUEUE_COLUMNS)}


def test_a_stage_with_no_declared_primary_key_says_so_rather_than_guessing(tmp_path, monkeypatch):
    """An `id` column is present and would have been guessed at by the removed
    join-key fallback; with no `primary_key` declared the page states that
    instead, and no column is flagged as the key."""
    project = "queue_route_no_primary_key"
    project_dir = tmp_path / project
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _no_primary_key_review_stage()],
    )

    described = review_routes._describe_queued_columns(
        _find_stage_def(project, "review"),
        loading.queue_snapshot(project, run_id, "review"),
    )

    assert described.identity_note == review_routes.NO_PRIMARY_KEY_NOTE
    assert not any(column.in_primary_key for column in described.columns)


def _schemaless_review_stage():
    return {"id": "review", "name": "Review items", "type": "human_review_queue",
            "inputs": [{"id": "load"}], "queue": dict(QUEUE_COLUMNS)}


def test_an_input_edge_with_no_schema_falls_back_to_the_queued_columns(tmp_path, monkeypatch):
    project = "queue_route_no_schema"
    project_dir = tmp_path / project
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _schemaless_review_stage()],
    )

    described = review_routes._describe_queued_columns(
        _find_stage_def(project, "review"),
        loading.queue_snapshot(project, run_id, "review"),
    )

    assert [column.name for column in described.columns] == ["id", "score"]
    assert all(column.description is None for column in described.columns)
    assert not any(column.in_primary_key for column in described.columns)
    assert described.schema_note == review_routes.NO_SCHEMA_NOTE


def test_a_never_opened_field_carries_the_value_it_displays(tmp_path, monkeypatch):
    """What a field submits when nobody touches it is `data-prefill`, and it is
    the same text the control displays — so an untouched submit records the
    value the reviewer was shown, which derives `approve`."""
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    field = re.search(r'<input[^>]*data-target="human_score"[^>]*>', html, re.DOTALL)
    assert field is not None
    prefill = re.search(r'\sdata-prefill="([^"]*)"', field.group(0))
    assert prefill is not None
    assert prefill.group(1) == _find_input_value(html, "human_score") == "1"


# ── 18. The card renders the queued row itself, described and linked ─────────
#
# The reviewable material is the snapshot row. Everything the card says about a
# column comes from the declared schema, and nothing on the page names a column
# this test's workflow did not declare.


def _described_review_stage():
    """The queue's input edge describes the columns it queues, and its output
    schema describes what the reviewer writes back."""
    return {"id": "review", "name": "Review labels", "type": "human_review_queue",
            "inputs": [{"id": "label", "schema": {
                "columns": [
                    {"name": "id", "type": "str"},
                    {"name": "score", "type": "int",
                     "description": "the score this row was labelled from"},
                    {"name": "label", "type": "str",
                     "description": "high when the score exceeds one"}],
                "primary_key": ["id"]}}],
            "output_schema": {"columns": [
                {"name": "id", "type": "str"}, {"name": "score", "type": "int"},
                {"name": "label", "type": "str"},
                {"name": "human_label", "type": "str",
                 "description": "the label after review"},
                {"name": "decision", "type": "str"},
                {"name": "reviewer_id", "type": "str"},
                {"name": "reviewed_at", "type": "str"},
                {"name": "review_notes", "type": "str", "nullable": True}]},
            "queue": {**queue_columns(source="label", target="human_label"),
                      "reviewer_instructions": "Confirm the label against the score."}}


def _described_queue_html(tmp_path, monkeypatch, project):
    project_dir = tmp_path / project
    run_id, fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _labelled_row_function_stage(),
         _described_review_stage()],
    )
    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text
    return run_id, fingerprints, html


def _first_card(html):
    card = html[html.index('<article class="queue-card'):]
    return card[:card.index("</article>")]


def test_the_card_renders_the_queued_row_as_a_key_value_table(tmp_path, monkeypatch):
    """Every context column, labelled by its own name — no `<pre>` JSON dump,
    no declared type on show, and no column name this workflow did not
    declare."""
    _run_id, _fingerprints, html = _described_queue_html(
        tmp_path, monkeypatch, "queue_route_kv_table")

    card = _first_card(html)
    assert '<table class="kv">' in card
    for column in ("id", "score"):
        assert f"<code>{column}</code>" in card
    assert "<pre>" not in card
    assert "type-pill" not in card
    for guessed in ("entity_id", "query_id", "benchmark_id", "quote", "benchmark_text"):
        assert guessed not in html


def test_a_declared_description_becomes_the_column_tooltip(tmp_path, monkeypatch):
    """And a column with no declared description gets NO tooltip — an absent
    description is stated by absence, never invented."""
    _run_id, _fingerprints, html = _described_queue_html(
        tmp_path, monkeypatch, "queue_route_kv_tooltip")

    card = _first_card(html)
    described = re.search(r'<th[^>]*title="([^"]*)"[^>]*>\s*<code>(\w+)</code>', card)
    assert described is not None
    assert described.groups() == ("the score this row was labelled from", "score")


def test_the_reviewed_field_label_carries_the_declared_description(tmp_path, monkeypatch):
    """The tooltip on `human_label` comes from the TARGET column's declared
    description in `output_schema`."""
    _run_id, _fingerprints, html = _described_queue_html(
        tmp_path, monkeypatch, "queue_route_field_tooltip")

    label = re.search(r'<label[^>]*for="[^"]*human_label"[^>]*>', html, re.DOTALL)
    assert label is not None
    assert 'title="the label after review"' in label.group(0)


def test_a_reviewed_field_falls_back_to_the_source_column_description(tmp_path, monkeypatch):
    """With no `output_schema` the target column is the source column, so the
    tooltip is the SOURCE column's declared description."""
    project = "queue_route_field_tooltip_source"
    project_dir = tmp_path / project
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _labelled_row_function_stage(), _review_labels_stage()],
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    label = re.search(r'<label[^>]*for="[^"]*human_label"[^>]*>', html, re.DOTALL)
    assert label is not None
    assert 'title="high when the score exceeds one"' in label.group(0)


def test_a_column_with_no_declared_description_carries_no_tooltip(tmp_path, monkeypatch):
    project = "queue_route_no_tooltip"
    project_dir = tmp_path / project
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _no_primary_key_review_stage()],
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    assert "title=" not in _first_card(html)


def test_the_card_header_states_the_row_position_and_the_lineage_link(tmp_path, monkeypatch):
    """A bare primary key identifies nothing to a human, so the header says
    where in the queue the reviewer is; the key itself stays in the table."""
    run_id, fingerprints, html = _described_queue_html(
        tmp_path, monkeypatch, "queue_route_card_header")

    positions = re.findall(r'<span class="row-position">([^<]*)</span>', html)
    assert positions == [f"Row {n} of {len(positions)}" for n in range(1, len(positions) + 1)]
    assert "identity-cell" not in html
    assert (f'href="/project/queue_route_card_header/runs/{run_id}/stage/label/row/'
            f'{fingerprints["row_ordinals"][0]}/trace/view"') in _first_card(html)


def test_a_stage_with_no_primary_key_states_it_rather_than_guessing_one(tmp_path, monkeypatch):
    project = "queue_route_identity_note"
    project_dir = tmp_path / project
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _no_primary_key_review_stage()],
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    assert review_routes.NO_PRIMARY_KEY_NOTE.replace("'", "&#39;") in html
    assert 'class="pk-flag"' not in html


def test_the_reviewer_instructions_are_not_rendered_as_a_code_block(tmp_path, monkeypatch):
    """They are the reviewer's brief, so they read as body text — the muted
    monospace `pre.instructions` styling belongs to the raw stage handle."""
    _run_id, _fingerprints, html = _described_queue_html(
        tmp_path, monkeypatch, "queue_route_instructions")

    assert '<pre class="instructions">' not in html
    assert '<p class="instructions-text">Confirm the label against the score.</p>' in html


def test_the_notes_box_is_labelled_and_invites_notes_on_any_decision(tmp_path, monkeypatch):
    """The notes column name is spelled out as a label, and nothing in the copy
    ties a note to a change — notes are as valid on an approval."""
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    assert "<span>Review notes</span>" in html  # the declared column is `review_notes`
    assert 'placeholder="Include any reasoning or citations for your decision"' in html
    assert "why you changed it" not in html


def test_the_notes_label_prefers_the_declared_description(tmp_path, monkeypatch):
    """With no declared description the column name is spelled out — an
    undeclared `reviewer_notes` reads "Reviewer notes", never a hardcoded one."""
    stage_def = Stage.model_validate(_described_review_stage())
    assert stage_def.output_schema is not None
    assert review_routes._resolve_notes_label(stage_def, "review_notes") == "Review notes"
    assert review_routes._resolve_notes_label(stage_def, "reviewer_notes") == "Reviewer notes"

    described = stage_def.model_copy(update={"output_schema": stage_def.output_schema.model_copy(
        update={"columns": [
            column.model_copy(update={"description": "Why you decided as you did"})
            if column.name == "review_notes" else column
            for column in stage_def.output_schema.columns]})})
    assert review_routes._resolve_notes_label(described, "review_notes") == (
        "Why you decided as you did")


def test_a_reviewed_value_is_read_only_until_its_edit_button_is_pressed(tmp_path, monkeypatch):
    """Both halves, as with the reviewer-name gate: the editor carries `hidden`,
    AND the stylesheet answers it. Without the second half the editor's own
    `display` beats the UA rule and every field is editable on load — markup-only
    assertions pass while the affordance is inert. The opener is a real
    `<button>`, which the platform activates on Enter and Space."""
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    editor = re.search(r'<span class="field-editor"[^>]*>', html)
    assert editor is not None and re.search(r"\bhidden\b", editor.group(0))
    opener = re.search(r'<button type="button" class="value-display"[^>]*>', html)
    assert opener is not None and "data-edit-for=" in opener.group(0)
    assert 'class="revert-edit"' in html

    stylesheet = (Path(app_package.__file__).parent / "static" / "style.css").read_text(
        encoding="utf-8"
    )
    assert re.search(r"\.field-control \[hidden\]\s*\{[^}]*display:\s*none", stylesheet)


def test_the_closed_field_displays_exactly_what_it_will_submit(tmp_path, monkeypatch):
    """The read-only display and `data-prefill` are the same text, so a field
    nobody opened submits the value the reviewer was shown."""
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    card = _first_card(html)
    shown = re.search(r'<span class="current-value">(.*?)</span>', card, re.DOTALL)
    assert shown is not None and shown.group(1).strip() == "1"
    assert 'data-prefill="1"' in card


def _empty_string_load_stage(project_dir):
    csv_path = project_dir / "data" / "rows.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("id,flag\ne,true\nn,false\n", encoding="utf-8")
    return {"id": "load", "name": "Load rows", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}},
            "output_schema": {"columns": [
                {"name": "id", "type": "str"},
                {"name": "flag", "type": "bool"}], "primary_key": ["flag"]}}


_EMPTY_STRING_COLUMNS = [
    {"name": "id", "type": "str"},
    {"name": "flag", "type": "bool"},
    {"name": "note", "type": "str", "nullable": True},
]


def _empty_string_row_function_stage():
    """One row holds a real empty string and one holds a null, in the same `str`
    column — the pair a null-flattening display prints alike. A CSV cannot carry
    the distinction (pandas reads a quoted empty field as NaN), so the frame is
    built by a row function."""
    code = ("def transform(row):\n"
            "    return {'id': row['id'], 'flag': row['flag'],\n"
            "            'note': '' if row['id'] == 'e' else None}")
    return {"id": "note", "name": "Add notes", "type": "python_row_function",
            "inputs": [{"id": "load", "schema": {
                "columns": _EMPTY_STRING_COLUMNS[:2], "primary_key": ["flag"]}}],
            "function": {"kind": "inline", "code": code},
            "output_schema": {"columns": _EMPTY_STRING_COLUMNS, "primary_key": ["flag"]}}


def _empty_string_review_stage():
    return {"id": "review", "name": "Review notes", "type": "human_review_queue",
            "inputs": [{"id": "note", "schema": {
                "columns": _EMPTY_STRING_COLUMNS, "primary_key": ["flag"]}}],
            "queue": {**queue_columns(source="flag", target="human_flag")}}


def test_an_empty_string_cell_is_not_printed_as_a_null(tmp_path, monkeypatch):
    """`display_cell` flattens a null to "", so a column holding a real empty
    string would otherwise be shown as holding nothing — stating something the
    data does not say."""
    project = "queue_route_empty_string"
    project_dir = tmp_path / project
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_empty_string_load_stage(project_dir), _empty_string_row_function_stage(),
         _empty_string_review_stage()],
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    cells = re.findall(r'<td class="kv-value">\s*(.*?)\s*</td>', html, re.DOTALL)
    assert "<em>empty string</em>" in cells
    assert "<em>null</em>" in cells


# ── 19. Context columns and reviewed columns are two different sections ──────


def test_a_reviewed_source_column_is_shown_only_in_the_review_section(tmp_path, monkeypatch):
    """The context table is the input row MINUS the columns under review: a
    column the reviewer is asked to change is shown once, beside its control,
    not twice. The subtraction is by the queue's declared `reviewed_columns`
    sources, never by matching on a column name."""
    _run_id, _fingerprints, html = _described_queue_html(
        tmp_path, monkeypatch, "queue_route_context_split")

    card = _first_card(html)
    table = card[card.index('<table class="kv">'):card.index("</table>")]
    labels = re.findall(r"<code>(\w+)</code>", table)
    assert labels == ["id", "score"]          # `label` is under review
    assert "received <code>label</code>:" in " ".join(card.split())


def _every_column_reviewed_stage():
    """A queue over a frame whose ONLY column is the one under review, so
    subtracting the reviewed columns leaves no context at all."""
    return {"id": "review", "name": "Review scores", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "score", "type": "int"}]}}],
            "queue": dict(QUEUE_COLUMNS)}


def test_no_context_table_is_rendered_when_every_column_is_under_review(tmp_path, monkeypatch):
    """No empty table, and no note inventing an explanation for its absence."""
    project = "queue_route_no_context"
    project_dir = tmp_path / project
    csv_path = project_dir / "data" / "scores.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("score\n1\n2\n", encoding="utf-8")
    load = {"id": "load", "name": "Load scores", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}},
            "output_schema": {"columns": [{"name": "score", "type": "int"}]}}
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project, [load, _every_column_reviewed_stage()])

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    assert '<table class="kv">' not in html
    assert "no columns" not in html
    assert 'data-target="human_score"' in html


# ── 20. A decided card is not asking for input ───────────────────────────────


def _decided_queue_html(tmp_path, monkeypatch):
    """The main fixture with its FIRST row decided through the real service."""
    project_dir, run_id, _run_dir, snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    _put_cached_decision(
        PROJECT, "review", fingerprints["stage_fingerprint"],
        fingerprints["input_fingerprints"][0], snapshot.iloc[0], ReviewVerdict.approve,
    )
    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text
    return project_dir, run_id, fingerprints, html


def test_a_decided_card_disables_its_openers_and_offers_a_secondary_cta(tmp_path, monkeypatch):
    """`disabled` on the `<button>` itself, so a keyboard user cannot tab into
    and activate it — a CSS-only look would leave the control live. And both
    halves of the hidden Submit: the attribute AND the stylesheet rule, without
    which `.btn`'s own `display` beats the UA's [hidden] rule."""
    _project_dir, _run_id, _fingerprints, html = _decided_queue_html(tmp_path, monkeypatch)

    decided = _first_card(html)
    opener = re.search(r'<button type="button" class="value-display"[^>]*>', decided)
    assert opener is not None and "disabled" in opener.group(0)
    assert ">Change my review<" in decided
    submit = re.search(r'<button type="submit" class="btn primary"[^>]*>', decided)
    assert submit is not None and re.search(r"\bhidden\b", submit.group(0))
    assert "Recorded: <strong>approve</strong>" in " ".join(decided.split())

    stylesheet = (Path(app_package.__file__).parent / "static" / "style.css").read_text(
        encoding="utf-8"
    )
    assert re.search(r"\.decision-controls \[hidden\]\s*\{[^}]*display:\s*none", stylesheet)


def test_an_undecided_card_offers_the_primary_submit_and_live_openers(tmp_path, monkeypatch):
    _project_dir, _run_id, fingerprints, html = _decided_queue_html(tmp_path, monkeypatch)

    undecided = html[html.index(f'data-input-fingerprint="{fingerprints["input_fingerprints"][1]}"'):]
    undecided = undecided[:undecided.index("</article>")]
    opener = re.search(r'<button type="button" class="value-display"[^>]*>', undecided)
    assert opener is not None and "disabled" not in opener.group(0)
    assert ">Change my review<" not in undecided
    submit = re.search(r'<button type="submit" class="btn primary"[^>]*>', undecided)
    assert submit is not None and not re.search(r"\bhidden\b", submit.group(0))


def test_unlocking_a_decided_card_records_a_new_verdict_on_resubmit(tmp_path, monkeypatch):
    """"Change my review" itself records nothing — it unlocks the card. The
    re-submit that follows still derives its verdict from the page's prefill,
    which on a decided row is the value the reviewer recorded before."""
    _project_dir, run_id, fingerprints, html = _decided_queue_html(tmp_path, monkeypatch)

    decided = _first_card(html)
    assert 'data-prefill="1"' in decided  # the recorded value the card opens on
    assert "data-unlock" in decided

    fp = fingerprints["input_fingerprints"][0]
    client = TestClient(app)
    changed = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fp, {"human_score": "7"}, prefilled={"human_score": "1"}),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["verdict"] == "modify"

    entry = StageCacheEntry.read_only().get(
        PROJECT, "review", fingerprints["stage_fingerprint"], fp)
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["human_score"] == 7
