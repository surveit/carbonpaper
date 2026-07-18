"""Tests for the review queue's model-input recovery (issue #49).

The queue page (`app/web/routers/review.py::queue_page`) has to join the queue
snapshot (the scoring stage's OUTPUT) back to the scoring stage's INPUT to show
the reviewer what the model actually saw. That join must never be guessed: a
guessed join key that turns out non-unique silently returns the wrong row
(last-write-wins), and the reviewer would approve/reject while looking at
evidence the model never scored. These tests pin down:

  - no primary_key declared on the scoring stage's input -> loud blind state,
    with a reason, no guessed fallback to evidence_id/entity_id/doc_id/id.
  - primary_key declared but not unique in the materialized input table -> loud
    blind state with a reason, no join performed.
  - the happy path (a real, unique primary key) -> model_input and
    rendered_prompt render correctly.
  - read_table / render_prompt failures surface their reason instead of
    silently becoming None.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.loading as loading
import app.web.routers.review as review_router
from app.main import app

PROJ = "revproj"
RUN = "run-0001"
STAGE = "review"  # the human_review_queue stage under test

client = TestClient(app)


def _compiled(project_dir: Path, score_input_schema: dict | None) -> None:
    """Three compiled stages: load (input_data) -> score (llm_transform) ->
    review (human_review_queue). `score_input_schema` is the `schema:` block on
    score's InputRef to `load` — None omits it entirely."""
    compiled = project_dir / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)

    load_stage = {
        "id": "load", "type": "input_data", "name": "Load evidence",
        "connector": {"kind": "file", "params": {"path": "data/evidence.csv", "format": "csv"}},
        "output_schema": {"columns": [
            {"name": "evidence_id", "type": "str"},
            {"name": "entity_id", "type": "str"},
            {"name": "quote", "type": "str"},
        ]},
    }
    score_input: dict = {"id": "load"}
    if score_input_schema is not None:
        score_input["schema"] = score_input_schema
    score_stage = {
        "id": "score", "type": "llm_transform", "name": "Score evidence",
        "inputs": [score_input],
        "llm": {"prompt_template": "Please score this quote: {quote}"},
        "output_schema": {"columns": [
            {"name": "evidence_id", "type": "str"},
            {"name": "entity_id", "type": "str"},
            {"name": "score", "type": "int"},
            {"name": "reasoning", "type": "str"},
        ]},
    }
    review_stage = {
        "id": STAGE, "type": "human_review_queue", "name": "Review scores",
        "inputs": [{"id": "score"}],
        "queue": {},
    }
    (compiled / "01_load.json").write_text(json.dumps(load_stage), encoding="utf-8")
    (compiled / "02_score.json").write_text(json.dumps(score_stage), encoding="utf-8")
    (compiled / "03_review.json").write_text(json.dumps(review_stage), encoding="utf-8")


def _write_run(project_dir: Path, load_df: pd.DataFrame, queue_df: pd.DataFrame) -> Path:
    """outputs/load.parquet (score stage's input) + queue/review.parquet (the
    queue snapshot, i.e. score stage's output) + a manifest tying them together."""
    run_dir = project_dir / "runs" / RUN
    (run_dir / "outputs").mkdir(parents=True)
    (run_dir / "queue").mkdir(parents=True)
    load_df.to_parquet(run_dir / "outputs" / "load.parquet", index=False)
    queue_df.to_parquet(run_dir / "queue" / f"{STAGE}.parquet", index=False)
    manifest = {
        "run_id": RUN,
        "status": "queued_for_review",
        "stages": [
            {"stage_id": "load", "status": "ok", "output_path": "outputs/load.parquet"},
            {"stage_id": "score", "status": "ok", "output_path": "queue/" + f"{STAGE}.parquet"},
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run_dir


@pytest.fixture()
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    return tmp_path / PROJ


def _queue_df() -> pd.DataFrame:
    """The queue snapshot: score stage's OUTPUT. Deliberately has no `quote`
    column of its own — the quote only lives upstream, in load's output — so
    the template's blind-review banner isn't masked by a same-named field."""
    return pd.DataFrame({
        "content_hash": ["h1", "h2"],
        "evidence_id": ["ev-1", "ev-2"],
        "entity_id": ["ent-A", "ent-A"],  # same entity for both -> not unique
        "score": [1, -1],
        "reasoning": ["looks relevant", "off-topic"],
    })


def _load_df() -> pd.DataFrame:
    return pd.DataFrame({
        "evidence_id": ["ev-1", "ev-2"],
        "entity_id": ["ent-A", "ent-A"],
        "quote": ["The company reported record revenue.", "Weather was mild."],
    })


# ── (a) no usable primary_key declared -> loud blind, no guessed fallback ────

def test_no_primary_key_declared_is_loud_blind(project_dir):
    _compiled(project_dir, score_input_schema=None)  # no schema at all on the InputRef
    _write_run(project_dir, _load_df(), _queue_df())

    r = client.get(f"/project/{PROJ}/runs/{RUN}/queue/{STAGE}")
    assert r.status_code == 200
    assert "Reviewing blind" in r.text
    assert "no primary_key declared on the scoring stage&#39;s input" in r.text \
        or "no primary_key declared on the scoring stage's input" in r.text
    # Must NOT silently guess entity_id (non-unique) and render a quote as if it joined.
    assert "The company reported record revenue." not in r.text
    assert "Weather was mild." not in r.text


def test_declared_pk_columns_missing_from_table_is_loud_blind(project_dir):
    # primary_key names a column that isn't actually in the materialized table.
    _compiled(project_dir, score_input_schema={
        "columns": [
            {"name": "evidence_id", "type": "str"},
            {"name": "entity_id", "type": "str"},
            {"name": "quote", "type": "str"},
            {"name": "doc_id", "type": "str"},
        ],
        "primary_key": ["doc_id"],
    })
    _write_run(project_dir, _load_df(), _queue_df())  # load_df has no doc_id column

    r = client.get(f"/project/{PROJ}/runs/{RUN}/queue/{STAGE}")
    assert r.status_code == 200
    assert "Reviewing blind" in r.text
    assert "not found in the input table" in r.text


# ── (b) declared PK present but not unique -> loud blind, no join performed ──

def test_non_unique_primary_key_is_loud_blind(project_dir):
    _compiled(project_dir, score_input_schema={
        "columns": [
            {"name": "evidence_id", "type": "str"},
            {"name": "entity_id", "type": "str"},
            {"name": "quote", "type": "str"},
        ],
        "primary_key": ["entity_id"],  # NOT unique: both evidence rows share ent-A
    })
    _write_run(project_dir, _load_df(), _queue_df())

    r = client.get(f"/project/{PROJ}/runs/{RUN}/queue/{STAGE}")
    assert r.status_code == 200
    assert "Reviewing blind" in r.text
    assert "not unique in the input table" in r.text
    assert "The company reported record revenue." not in r.text
    assert "Weather was mild." not in r.text


# ── (c) happy path: unique PK join renders model_input + rendered_prompt ─────

def test_unique_primary_key_renders_model_input_and_prompt(project_dir):
    _compiled(project_dir, score_input_schema={
        "columns": [
            {"name": "evidence_id", "type": "str"},
            {"name": "entity_id", "type": "str"},
            {"name": "quote", "type": "str"},
        ],
        "primary_key": ["evidence_id"],  # unique
    })
    _write_run(project_dir, _load_df(), _queue_df())

    r = client.get(f"/project/{PROJ}/runs/{RUN}/queue/{STAGE}")
    assert r.status_code == 200
    assert "Reviewing blind" not in r.text
    # Each row joined to the RIGHT upstream quote, not a last-row-wins guess.
    assert "The company reported record revenue." in r.text
    assert "Weather was mild." in r.text
    assert "Please score this quote: The company reported record revenue." in r.text
    assert "Please score this quote: Weather was mild." in r.text


# ── (d) swallowed exceptions now surface a reason ────────────────────────────

def test_read_table_failure_surfaces_reason(project_dir, monkeypatch):
    _compiled(project_dir, score_input_schema={
        "columns": [
            {"name": "evidence_id", "type": "str"},
            {"name": "entity_id", "type": "str"},
            {"name": "quote", "type": "str"},
        ],
        "primary_key": ["evidence_id"],
    })
    _write_run(project_dir, _load_df(), _queue_df())

    def _boom(path):
        raise ValueError("corrupt parquet footer")

    monkeypatch.setattr(review_router, "read_table", _boom)

    r = client.get(f"/project/{PROJ}/runs/{RUN}/queue/{STAGE}")
    assert r.status_code == 200
    assert "Reviewing blind" in r.text
    assert "could not read upstream input table" in r.text
    assert "corrupt parquet footer" in r.text


def test_render_prompt_failure_surfaces_reason_but_keeps_model_input(project_dir, monkeypatch):
    _compiled(project_dir, score_input_schema={
        "columns": [
            {"name": "evidence_id", "type": "str"},
            {"name": "entity_id", "type": "str"},
            {"name": "quote", "type": "str"},
        ],
        "primary_key": ["evidence_id"],
    })
    _write_run(project_dir, _load_df(), _queue_df())

    def _boom(template, row):
        raise RuntimeError("template blew up")

    monkeypatch.setattr(review_router, "render_prompt", _boom)

    r = client.get(f"/project/{PROJ}/runs/{RUN}/queue/{STAGE}")
    assert r.status_code == 200
    # The join itself succeeded -- we're not reviewing blind, the model input
    # (the quote) is still shown -- but the exact-prompt render failure is named.
    assert "Reviewing blind" not in r.text
    assert "The company reported record revenue." in r.text
    assert "Couldn't recover the exact prompt because" in r.text
    assert "template blew up" in r.text
