# Queue snapshots here are genuine runner output, not fixtures.
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app as app_package
import app.runtime.runner as runner
import app.web.loading as loading
from app.services import workspace
from app.web import queue_view
from app.main import app
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stages import llm_transform as lt
from app.services import review, versioning
from app.core.stage_cache import StageCacheEntry
from app.services.project import save_working_copy_as_version
from app.models import Stage, parse_stage
from app.models.stages.human_review_queue import ReviewVerdict
from conftest import (
    QUEUE_COLUMNS, pinned_stages, queue_added_columns, queue_columns, resumed_stages,
)

PROJECT = "queue_route_journey"


def _seed_version(root):
    vid = save_working_copy_as_version(root, message="test seed", reviewer="test").version_id
    versioning.publish_version(root, vid, reviewer="human")


def _with_queue_signature(stage):
    input_schema = stage["inputs"][0]["schema"]
    # `stage` plus the signature its `queue` block implies: each reviewed source
    # repeated under its target name and spec, then the review-record columns.
    # For the fixtures whose subject is something other than the signature.
    by_name = {column["name"]: column for column in input_schema["columns"]}
    queue = stage["queue"]
    added = [{**by_name[source], "name": target}
             for source, target in queue["reviewed_columns"].items()]
    added += [{"name": queue[field], "type": "str", "nullable": True}
              for field in ("verdict_column", "reviewer_column",
                            "reviewed_at_column", "review_notes_column")
              if queue.get(field) is not None]
    return {**stage, "signature": {"form": "extends", "adds": added}}


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
    return {"id": "load", "description": "Load quotes", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}},
            "signature": {
                "form": "replaces",
                "produces": [
                    {"name": "id", "type": "str", "nullable": True},
                    {"name": "quote", "type": "str", "nullable": True},
                ],
            }}


# The reviewer columns app/services/review.py's _build_output_row (and the
# runtime's pass-through/auto-approve rows) add on top of the frozen input row.
# Every non-publish stage's signature must say what it outputs
# (app/models/stage.py: Stage._schemas_declared), and the runtime PROJECTS the
# stage's output onto exactly those columns.
_REVIEW_COLUMNS = queue_added_columns()


def _score_stage():
    # llm_transform: scores each quote. The signature is additive (a stage invariant —
    # app/models/stage.py's _llm_transform_one_to_one), so `quote` survives onto the
    # queued row.
    return {"id": "score", "description": "Score quotes", "type": "llm_transform",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "quote", "type": "str", "nullable": True}]}}],
            "signature": {
                "form": "extends",
                "reads": [
                    {
                        "input": "load",
                        "columns": [{"name": "quote", "type": "str", "nullable": True}],
                    },
                ],
                "adds": [{"name": "score", "type": "int", "nullable": False}],
            },
            "llm": {"prompt_instructions": "Score each quote for tone.",
                    "prompt_data_template": "Rate this: {quote}"}}


def _review_stage():
    # human_review_queue reviewing `score`'s output; no cached decisions yet, so the run
    # halts and snapshots both rows.
    return {"id": "review", "description": "Review scores", "type": "human_review_queue",
            "inputs": [{"id": "score", "schema": {
                "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "quote", "type": "str", "nullable": True},
                            {"name": "score", "type": "int", "nullable": True}]}}],
            "signature": {"form": "extends", "adds": _REVIEW_COLUMNS},
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
    # The form a browser posts to /decide. `prefilled` defaults to `reviewed`, the
    # unchanged submit the endpoint settles as `approve`; pass a different mapping to post
    # a changed value. Either may be a raw string, for the malformed-payload cases.
    if prefilled is None:
        prefilled = {} if isinstance(reviewed, str) else reviewed
    return {
        "input_fingerprint": fp, "reviewer": reviewer,
        "reviewed_values": reviewed if isinstance(reviewed, str) else json.dumps(reviewed),
        "prefilled_values": prefilled if isinstance(prefilled, str) else json.dumps(prefilled),
        **extra,
    }


def _build_and_halt(tmp_path, monkeypatch):
    # Builds load -> score (llm_transform, mocked) -> review (human_review_queue) and runs
    # it for real. The run halts at `review` with both rows snapshotted. Returns
    # (project_dir, run_id, run_dir, snapshot, fingerprints) — fingerprints is the sidecar
    # dict, its `input_fingerprints` list POSITIONALLY aligned to `snapshot`'s row order.
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(
        lt, "call_llm", lambda stage_id, llm_config, row, **kw: {"score": 1}
    )

    project_dir = tmp_path / PROJECT
    _write_stage(project_dir, "01_load.json", _load_quotes_stage(project_dir))
    _write_stage(project_dir, "02_score.json", _score_stage())
    _write_stage(project_dir, "03_review.json", _review_stage())
    _seed_version(project_dir)

    manifest = run_prepared(prepare_run(project_dir, project_dir, *pinned_stages(project_dir)))
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
    # Seed a prior decision through the real review service (record_decision → the
    # production cache seam) — never a hand-assembled entry, a raw store write, or the
    # HTTP endpoint (used by tests that only care about queue_page's rendering of an
    # already-cached decision). `reviewed_score` is the reviewer's value for
    # QUEUE_COLUMNS' one reviewed column, defaulting to the AI value they were shown.
    review.record_decision(
        project=project, stage=parse_stage(_review_stage()),
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        frozen_row={"id": row["id"], "quote": row["quote"], "score": int(row["score"])},
        verdict=decision,
        reviewed_values={
            "human_score": int(row["score"]) if reviewed_score is None else reviewed_score
        },
        review_notes=None,
        reviewer="local", reviewed_at="2026-07-01T00:00:00",
    )


# ── 1. The page a halted queue renders: rows, prior decisions, controls ─────


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
    # One field per declared reviewed column, typed from the declared column and
    # pre-filled with the value the reviewer is being asked to confirm or change.
    assert 'data-target="human_score"' in html
    assert 'type="number"' in html
    assert 'data-prefill="1"' in html  # the mocked upstream score
    # One Submit per card; the verdict is settled server-side, so no button names one.
    assert html.count(">Submit<") == html.count('<article class="queue-card')
    # The notes column is labelled by name, and nothing ties a note to a change.
    assert "<span>Review notes</span>" in html  # the declared column is `review_notes`
    assert 'placeholder="Include any reasoning or citations for your decision"' in html


# ── 2. Route-level refusals: 404 on the stage, 400 on the payload ───────────


def test_404_when_the_stage_id_is_not_a_human_review_queue_stage(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    r = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/load")  # `load` is input_data

    assert r.status_code == 404


@pytest.mark.parametrize(
    "reviewed, prefilled, extra, expected_in_detail",
    [
        # The payload itself is unreadable as a map of target column -> value.
        ("{not json", None, {}, "not valid JSON"),
        ("[1, 2]", None, {}, "JSON object"),
        # The verdict is settled by comparing the two maps column by column, so a
        # column in only one is a comparison that cannot be made — never one
        # silently treated as unchanged.
        ({"human_score": 1}, {}, {}, "human_score"),
        # The review service is the sole authority on the key set.
        ({}, {}, {}, "human_score"),
        ({"human_score": 1, "smuggled": "x"}, None, {}, "smuggled"),
        # A value the declared column refuses: named, with the offending text.
        ({"human_score": "banana"}, {"human_score": "1"}, {}, "banana"),
        # No decision is recorded unattributed.
        ({"human_score": 1}, None, {"reviewer": "   "}, "reviewer"),
    ],
)
def test_decide_400s_and_writes_nothing_on_a_payload_it_cannot_honour(
    tmp_path, monkeypatch, reviewed, prefilled, extra, expected_in_detail
):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    r = TestClient(app).post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fp, reviewed, prefilled=prefilled, **extra),
    )

    assert r.status_code == 400, r.text
    assert expected_in_detail in r.json()["detail"]
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")  # nothing written


def test_decide_404_on_unknown_fingerprint_and_writes_nothing(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    client = TestClient(app)
    r = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data("not-a-real-fingerprint", {"human_score": 1}),
    )
    assert r.status_code == 404
    assert not StageCacheEntry.list(prefix=f"{PROJECT}/review/")


# ── 3. The verdict is SETTLED from submitted vs pre-filled values ───────────


@pytest.mark.parametrize(
    "submitted, extra, expected_verdict, expected_stored",
    [
        ("1", {"reviewer": "  Ada Lovelace  "}, "approve", 1),
        ("4", {}, "modify", 4),
        # The verdict comes from what changed, so a `verdict` field on the form
        # is inert — `skipped` (the runtime's own verdict, which the review
        # service refuses: tests/services/test_review.py) cannot be smuggled in.
        ("1", {"verdict": "skipped"}, "approve", 1),
        # Form text is parsed to the declared type, never stored as a string.
        ("  -3 ", {}, "modify", -3),
    ],
)
def test_decide_settles_the_verdict_from_submitted_against_prefilled(
    tmp_path, monkeypatch, submitted, extra, expected_verdict, expected_stored
):
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    r = TestClient(app).post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fp, {"human_score": submitted}, prefilled={"human_score": "1"}, **extra),
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == expected_verdict

    entry = StageCacheEntry.read_only().get(
        PROJECT, "review", fingerprints["stage_fingerprint"], fp)
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["decision"] == expected_verdict
    assert entry.output_row["human_score"] == expected_stored
    assert not isinstance(entry.output_row["human_score"], str)
    # The reviewer is the name the form posted, trimmed — never a hardcoded one.
    assert entry.output_row["reviewer_id"] == extra.get("reviewer", "Ada").strip()


def test_snapshot_columns_are_exactly_the_upstream_columns(tmp_path, monkeypatch):
    _project_dir, _run_id, _run_dir, snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)
    assert set(snapshot.columns) == {"id", "quote", "score"}


# ── 4. End-to-end capstone: decide every verdict, then resume ───────────────


def _e2e_load_stage(root):
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "items.csv"
    pd.DataFrame({"id": ["a", "b", "c"], "score": [1, 2, 3]}).to_csv(csv_path, index=False)
    return {"id": "load", "description": "Load items", "type": "input_data",
            "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}},
            "signature": {
                "form": "replaces",
                "produces": [
                    {"name": "id", "type": "str", "nullable": True},
                    {"name": "score", "type": "int", "nullable": True},
                ],
            }}


def _e2e_review_stage():
    return {"id": "review", "description": "Review items", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True}]}}],
            "signature": {"form": "extends", "adds": _REVIEW_COLUMNS},
            "queue": dict(QUEUE_COLUMNS)}


def test_e2e_decide_every_verdict_then_resume_completes(tmp_path, monkeypatch):
    # halt -> POST /decide for each pending row -> runner.resume_run -> completed
    # manifest, with the resumed output reflecting each SETTLED verdict: a submit matching
    # the prefill records approve and keeps the AI score, one differing from it records
    # modify and substitutes the human-entered score. Every reviewed row is emitted. No
    # decisions/ directory is created under the project dir — every write goes through the
    # cache.
    project = "queue_route_e2e"
    workspace.set_projects_dir(tmp_path)

    project_dir = tmp_path / project
    _write_stage(project_dir, "01_load.json", _e2e_load_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _e2e_review_stage())
    _seed_version(project_dir)

    manifest = run_prepared(prepare_run(project_dir, project_dir, *pinned_stages(project_dir)))
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
    # unchanged settles approve, submitting anything else settles modify.
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

    resumed = runner.resume_run(project_dir, run_id, project_dir,
                                  *resumed_stages(project_dir, run_id))
    assert resumed["status"] == "ok"

    out = pd.read_parquet(run_dir / "outputs" / "review.parquet").set_index("id")
    assert list(out.index) == ["a", "b", "c"]   # every reviewed row is emitted
    assert out.loc["a", "human_score"] == 1     # approve: AI score kept
    assert out.loc["b", "human_score"] == 99    # modify: human score used
    assert out.loc["c", "decision"] == "modify"  # the row stays, carrying its verdict
    assert out.loc["b", "reviewer_id"] == "Ada Reviewer"  # the name the reviewer typed
    assert out.loc["c", "human_score"] == 0      # a human-entered 0 is a score, not a blank

    assert not (project_dir / "decisions").exists()


# ── 5. Review notes: normalised at the boundary, refused with no column ─────


def test_decide_accepts_an_untouched_notes_box_as_no_note(tmp_path, monkeypatch):
    # An HTML form posts an empty textarea as "" — that is no note, not an empty note, and
    # must not reach the notes column as one.
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
    return _with_queue_signature({
            "id": "review", "description": "Review items", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True}]}}],
            "queue": queue})


def test_decide_400_on_notes_when_the_stage_declares_no_notes_column(tmp_path, monkeypatch):
    project = "queue_route_no_notes"
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / project
    _write_stage(project_dir, "01_load.json", _e2e_load_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _no_notes_review_stage())
    _seed_version(project_dir)

    manifest = run_prepared(prepare_run(project_dir, project_dir, *pinned_stages(project_dir)))
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


# ── 6. Live-definition drift from the halted run ────────────────────────────


def _drift_the_review_stage(project_dir):
    # Rename the reviewed target on the LIVE definition after the run halted, so the live
    # stage fingerprint no longer matches the sidecar's.
    drifted = _review_stage()
    drifted["queue"] = {**QUEUE_COLUMNS, "reviewed_columns": {"score": "checked_score"}}
    drifted["signature"] = {**drifted["signature"], "adds": [
        {"name": "checked_score", "type": "int", "nullable": True} if column["name"] == "human_score" else column
        for column in drifted["signature"]["adds"]
    ]}
    _write_stage(project_dir, "03_review.json", drifted)


def test_queue_page_states_the_drift_and_renders_no_items(tmp_path, monkeypatch):
    # The run's decisions are keyed by the fingerprint it halted under, but the columns
    # they are read and written through come from the live definition. Once the two
    # describe different column sets the page says so in place of the rows, rather than
    # raising KeyError or half-rendering them.
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


# ── 7. The reviewer-name gate, and what a decided row's field opens on ──────


def test_queue_page_gates_the_items_behind_the_reviewer_name(tmp_path, monkeypatch):
    # The gate takes BOTH halves: the container carries `hidden`, and the stylesheet
    # answers it with an [hidden] rule. Without the second half the container's own
    # `display: flex` beats the UA rule and the rows render anyway, so asserting the
    # markup alone passes while the gate is visually inert.
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    assert 'id="reviewer-name"' in html
    container = re.search(r"<div[^>]*id=\"queue-items\"[^>]*>", html)
    assert container is not None and re.search(r"\bhidden\b", container.group(0))

    stylesheet = "\n".join(
        sheet.read_text(encoding="utf-8")
        for sheet in sorted((Path(app_package.__file__).parent / "static").glob("*.css"))
    )
    assert re.search(r"\.queue-items\[hidden\]\s*\{[^}]*display:\s*none", stylesheet)


def test_queue_page_prefills_a_decided_row_from_the_recorded_value(tmp_path, monkeypatch):
    # A decided row opens with what the reviewer recorded, not the value it contradicts —
    # otherwise an untouched submit silently reverts their own decision. What the stage
    # received stays visible beside it, labelled separately.
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


# ── 8. Controls whose value has its own spelling: bool, date/datetime, range ─


def _bool_review_stage(nullable):
    return _with_queue_signature({
        "id": "review", "description": "Review flags", "type": "human_review_queue",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str", "nullable": True},
                        {"name": "flag", "type": "bool", "nullable": nullable}]}}],
        "queue": {**queue_columns(source="flag", target="human_flag")}})


def _build_and_halt_bool_queue(tmp_path, monkeypatch, project, *, ai_value, nullable=True):
    # A one-row queue over a `bool` column whose AI value is `ai_value` (None for a null).
    # Returns (project, run_id, fingerprints, snapshot).
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / project
    (project_dir / "data").mkdir(parents=True, exist_ok=True)
    csv_path = project_dir / "data" / "flags.csv"
    pd.DataFrame({"id": ["a"], "flag": [ai_value]}).to_csv(csv_path, index=False)
    _write_stage(project_dir, "01_load.json", {
        "id": "load", "description": "Load flags", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}},
        "signature": {"form": "replaces", "produces": [
            {"name": "id", "type": "str", "nullable": True},
            {"name": "flag", "type": "bool", "nullable": nullable}]}})
    _write_stage(project_dir, "02_review.json", _bool_review_stage(nullable))
    _seed_version(project_dir)
    run_id = run_prepared(prepare_run(project_dir, project_dir, *pinned_stages(project_dir)))["run_id"]
    run_dir = project_dir / "runs" / run_id
    return run_id, _read_fingerprints(run_dir), pd.read_parquet(run_dir / "queue" / "review.parquet")


def _find_selected_option(html, target):
    # The value of the `selected` option of `target`'s select, or None when the select
    # pre-selects nothing — in which case a browser falls back to whichever option happens
    # to be FIRST, so "nothing selected" is never a safe state.
    select = re.search(
        rf'<select[^>]*data-target="{target}"[^>]*>(.*?)</select>', html, re.DOTALL
    )
    assert select is not None, f"no select rendered for {target!r}"
    chosen = re.search(r'<option value="([^"]*)"[^>]*\bselected\b', select.group(1))
    return None if chosen is None else chosen.group(1)


def test_a_null_bool_ai_value_is_never_rendered_as_false(tmp_path, monkeypatch):
    # A checkbox has two states and a nullable bool has three, so a checkbox would
    # advertise a missing AI value as `false` and Approve would post it. The field is a
    # select that opens EXPLICITLY on its unset option — not merely without a selection,
    # which would leave the browser showing `true`.
    run_id, _fingerprints, _snapshot = _build_and_halt_bool_queue(
        tmp_path, monkeypatch, "queue_route_bool_null", ai_value=None)

    html = TestClient(app).get(f"/project/queue_route_bool_null/runs/{run_id}/queue/review").text

    assert 'type="checkbox"' not in html
    # the absent upstream value shown as absent, not as "false"
    assert "received <code>flag</code>: <strong><em>no value</em></strong>" in " ".join(html.split())
    assert "— unset —" in html
    assert _find_selected_option(html, "human_flag") == ""


def test_a_bool_select_opens_on_the_recorded_value_of_a_decided_row(tmp_path, monkeypatch):
    # The recorded `true` must come back SELECTED. Left unselected, the unset option
    # renders first, the browser shows it, and an untouched Save posts "" — silently
    # reverting the reviewer's own decision.
    project = "queue_route_bool_recorded"
    run_id, fingerprints, snapshot = _build_and_halt_bool_queue(
        tmp_path, monkeypatch, project, ai_value=False)
    review.record_decision(
        project=project, stage=parse_stage(_bool_review_stage(True)),
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
    # With no unset option to fall back on, an unselected select shows whichever option is
    # FIRST — `true` — so a row the model said `false` for would record `true` on an
    # untouched Save. The AI value must be selected.
    project = "queue_route_bool_required"
    run_id, fingerprints, _snapshot = _build_and_halt_bool_queue(
        tmp_path, monkeypatch, project, ai_value=False, nullable=False)

    client = TestClient(app)
    html = client.get(f"/project/{project}/runs/{run_id}/queue/review").text
    assert "— unset —" not in html                              # nothing to be unset to
    assert _find_selected_option(html, "human_flag") == "false"

    # Submitting what the page opened on records `false`, not the first option
    # — and, unchanged from the prefill, settles `approve`.
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


def _temporal_review_stage(column_type):
    return _with_queue_signature({
        "id": "review", "description": "Review times", "type": "human_review_queue",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str", "nullable": True},
                        {"name": "seen_at", "type": column_type, "nullable": True}]}}],
        "queue": {**queue_columns(source="seen_at", target="human_seen_at")}})


def _decide_a_temporal_row(tmp_path, monkeypatch, project, column_type, recorded):
    # Runs a one-row queue over a `date`/`datetime` column, records `recorded` for it
    # through the real endpoint, and returns the reloaded page.
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / project
    (project_dir / "data").mkdir(parents=True, exist_ok=True)
    csv_path = project_dir / "data" / "sightings.csv"
    pd.DataFrame({"id": ["a"], "seen_at": ["2026-01-01T08:00:00"]}).to_csv(csv_path, index=False)
    _write_stage(project_dir, "01_load.json", {
        "id": "load", "description": "Load sightings", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}},
        "signature": {"form": "replaces", "produces": [
            {"name": "id", "type": "str", "nullable": True},
            {"name": "seen_at", "type": column_type, "nullable": True}]}})
    _write_stage(project_dir, "02_review.json", _temporal_review_stage(column_type))
    _seed_version(project_dir)
    run_id = run_prepared(prepare_run(project_dir, project_dir, *pinned_stages(project_dir)))["run_id"]
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


@pytest.mark.parametrize(
    "column_type, control, recorded",
    [("datetime", "datetime-local", "2026-03-04T09:30:00"), ("date", "date", "2026-03-04")],
)
def test_a_temporal_control_opens_on_the_recorded_value_of_a_decided_row(
    tmp_path, monkeypatch, column_type, control, recorded
):
    # The recorded value comes back from the cache stringified — space- separated, which a
    # `date`/`datetime-local` control rejects, rendering BLANK on a row that has a value.
    # An untouched Save would then post "" over it.
    html = _decide_a_temporal_row(
        tmp_path, monkeypatch, f"queue_route_{column_type}", column_type, recorded)

    assert f'type="{control}"' in html
    assert _find_input_value(html, "human_seen_at") == recorded


def _declared_range_review_stage():
    """`human_score` resolves from the signature, not the input edge's `score`."""
    return {"id": "review", "description": "Review items", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str", "nullable": True},
                            {"name": "score", "type": "int", "nullable": False,
                             "range": [0, 5]}]}}],
            "signature": {
                "form": "extends",
                "adds": [
                    {
                        "name": "human_score",
                        "type": "int",
                        "nullable": True,
                        "range": [0, 5],
                    },
                    {"name": "decision", "type": "str", "nullable": True},
                    {"name": "reviewer_id", "type": "str", "nullable": True},
                    {"name": "reviewed_at", "type": "str", "nullable": True},
                    {"name": "review_notes", "type": "str", "nullable": True},
                ],
            },
            "queue": dict(QUEUE_COLUMNS)}


def _build_and_halt_declared_range_queue(tmp_path, monkeypatch, project):
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / project
    # The loader declares `score` exactly as the review stage's edge does: the
    # edge check requires the producer to be no more permissive than the consumer.
    load = _e2e_load_stage(project_dir)
    load["signature"] = {"form": "replaces", "produces": [
        {"name": "id", "type": "str", "nullable": True},
        {"name": "score", "type": "int", "nullable": False, "range": [0, 5]}]}
    _write_stage(project_dir, "01_load.json", load)
    _write_stage(project_dir, "02_review.json", _declared_range_review_stage())
    _seed_version(project_dir)
    run_id = run_prepared(prepare_run(project_dir, project_dir, *pinned_stages(project_dir)))["run_id"]
    return run_id, _read_fingerprints(project_dir / "runs" / run_id)


def test_decide_coerces_against_the_signature_column_when_declared(tmp_path, monkeypatch):
    project = "queue_route_output_schema"
    run_id, fingerprints = _build_and_halt_declared_range_queue(tmp_path, monkeypatch, project)
    fp = fingerprints["input_fingerprints"][0]

    client = TestClient(app)
    html = client.get(f"/project/{project}/runs/{run_id}/queue/review").text
    assert 'min="0"' in html and 'max="5"' in html  # the declared range, on the control

    url = f"/project/{project}/runs/{run_id}/queue/review/decide"
    refused = client.post(url, data=_decide_data(
        fp, {"human_score": 9}, prefilled={"human_score": 1}))
    assert refused.status_code == 400  # outside the declared [0, 5]
    detail = refused.json()["detail"]
    assert "human_score" in detail and "less than or equal to 5" in detail

    # Blank is a null only because the signature's `human_score` is nullable; the
    # input edge's `score` is not, so this is the signature path being read.
    accepted = client.post(url, data=_decide_data(
        fp, {"human_score": ""}, prefilled={"human_score": 1}))
    assert accepted.status_code == 200, accepted.text
    entry = StageCacheEntry.read_only().get(
        project, "review", fingerprints["stage_fingerprint"], fp)
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["human_score"] is None


# ── 9. The queued rows are described from the DECLARED input schema ─────────
#
# `human_review_queue` runs for any workflow, so nothing here may depend on the
# upstream stage's type or on any particular column name.


def _build_and_halt_queue_over(tmp_path, monkeypatch, project, stages):
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / project
    for index, stage in enumerate(stages, start=1):
        _write_stage(project_dir, f"{index:02d}_{stage['id']}.json", stage)
    _seed_version(project_dir)
    manifest = run_prepared(prepare_run(project_dir, project_dir, *pinned_stages(project_dir)))
    assert manifest["status"] == "awaiting_review", manifest
    run_id = manifest["run_id"]
    return run_id, _read_fingerprints(project_dir / "runs" / run_id)


def _lineage_urls(project, run_id, stage_id="review"):
    stage_def = _find_stage_def(project, stage_id)
    fingerprints = loading.load_queue_fingerprints(project, run_id, stage_id)
    assert fingerprints is not None
    return queue_view.build_lineage_urls(
        project, run_id, queue_view.resolve_lineage(stage_def, fingerprints), fingerprints
    )


def test_a_queue_directly_on_input_data_renders_and_links_to_that_stage(tmp_path, monkeypatch):
    # The snapshot IS the material to review here — there is no model between the input
    # and the queue — so the page renders in full and traces the `input_data` stage's row.
    project = "queue_route_on_input_data"
    project_dir = tmp_path / project
    run_id, fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _e2e_review_stage()],
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text
    for fp in fingerprints["input_fingerprints"]:
        assert f'data-input-fingerprint="{fp}"' in html

    assert _lineage_urls(project, run_id) == [
        f"/project/{project}/runs/{run_id}/stage/load/row/{o}/trace/view"
        for o in fingerprints["row_ordinals"]
    ]


def _labelled_row_function_stage():
    # python_row_function upstream: no model produced these values, and the queue's input
    # edge declares a description for the column it adds.
    code = (
        "def transform(row):\n"
        "    return {'id': row['id'], 'score': row['score'],\n"
        "            'label': 'high' if row['score'] > 1 else 'low'}"
    )
    return {"id": "label", "description": "Label items", "type": "python_row_function",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True}]}}],
            "function": {"kind": "inline", "code": code},
            "signature": {
                "form": "extends",
                "reads": [
                    {
                        "input": "load",
                        "columns": [
                            {"name": "id", "type": "str", "nullable": True},
                            {"name": "score", "type": "int", "nullable": True},
                        ],
                    },
                ],
                "adds": [{"name": "label", "type": "str", "nullable": True}],
            }}


def _review_labels_stage():
    return _with_queue_signature({
        "id": "review", "description": "Review labels", "type": "human_review_queue",
        "inputs": [{"id": "label", "schema": {
            "columns": [
                {"name": "id", "type": "str", "nullable": True},
                {"name": "score", "type": "int", "nullable": True},
                {"name": "label", "type": "str",
                 "description": "high when the score exceeds one", "nullable": True}]}}],
        "queue": {**queue_columns(source="label", target="human_label")}})


def test_a_queue_whose_upstream_is_not_an_llm_transform_renders_and_links(tmp_path, monkeypatch):
    # A `python_row_function` computed these values, so nothing on the page may attribute
    # them to a model: the queue stage cannot know what produced what it received, and
    # telling the reviewer "AI" is exactly the fabrication this surface was rewritten to
    # remove.
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


def _described_review_stage():
    # The queue's input edge describes the columns it queues, and its output schema
    # describes what the reviewer writes back.
    return {"id": "review", "description": "Review labels", "type": "human_review_queue",
            "inputs": [{"id": "label", "schema": {
                "columns": [
                    {"name": "id", "type": "str", "nullable": True},
                    {"name": "score", "type": "int",
                     "description": "the score this row was labelled from", "nullable": True},
                    {"name": "label", "type": "str",
                     "description": "high when the score exceeds one", "nullable": True}]}}],
            "signature": {
                "form": "extends",
                "adds": [
                    {
                        "name": "human_label",
                        "type": "str",
                        "description": "the label after review",
                        "nullable": True,
                    },
                    {"name": "decision", "type": "str", "nullable": True},
                    {"name": "reviewer_id", "type": "str", "nullable": True},
                    {"name": "reviewed_at", "type": "str", "nullable": True},
                    {"name": "review_notes", "type": "str", "nullable": True},
                ],
            },
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


def test_the_card_renders_the_described_queued_row_and_its_review_section(tmp_path, monkeypatch):
    # The context table is the queued row MINUS the columns under review, each column
    # labelled by its own name and tooltipped from its DECLARED description — no `<pre>`
    # JSON dump, no declared type on show, no invented tooltip, and no column name this
    # workflow did not declare. The header says where in the queue the reviewer is (a bare
    # primary key identifies nothing to a human) and links the upstream row; the reviewer
    # brief reads as body text.
    run_id, fingerprints, html = _described_queue_html(
        tmp_path, monkeypatch, "queue_route_card")

    card = _first_card(html)
    assert '<table class="kv">' in card
    assert "<pre>" not in card
    assert "type-pill" not in card
    for guessed in ("entity_id", "query_id", "benchmark_id", "quote", "benchmark_text"):
        assert guessed not in html

    table = card[card.index('<table class="kv">'):card.index("</table>")]
    assert re.findall(r"<code>(\w+)</code>", table) == ["id", "score"]  # `label` is reviewed
    assert "received <code>label</code>:" in " ".join(card.split())

    # A declared description becomes the tooltip; `id` declares none and gets none.
    described = re.search(r'<th[^>]*title="([^"]*)"[^>]*>\s*<code>(\w+)</code>', card)
    assert described is not None
    assert described.groups() == ("the score this row was labelled from", "score")
    assert re.search(r'<th[^>]*>\s*<code>id</code>', table) is not None
    label = re.search(r'<label[^>]*for="[^"]*human_label"[^>]*>', html, re.DOTALL)
    assert label is not None and 'title="the label after review"' in label.group(0)

    positions = re.findall(r'<span class="row-position">([^<]*)</span>', html)
    assert positions == [f"Row {n} of {len(positions)}" for n in range(1, len(positions) + 1)]
    assert "identity-cell" not in html
    assert (f'href="/project/queue_route_card/runs/{run_id}/stage/label/row/'
            f'{fingerprints["row_ordinals"][0]}/trace/view"') in card

    # The reviewer brief is their brief, not the raw stage handle's monospace.
    assert '<pre class="instructions">' not in html
    assert '<p class="instructions-text">Confirm the label against the score.</p>' in html


def test_a_reviewed_value_is_read_only_until_its_edit_button_is_pressed(tmp_path, monkeypatch):
    # Both halves, as with the reviewer-name gate: the editor carries `hidden`, AND the
    # stylesheet answers it. Without the second half the editor's own `display` beats the
    # UA rule and every field is editable on load — markup-only assertions pass while the
    # affordance is inert. The opener is a real `<button>`, which the platform activates
    # on Enter and Space.
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    editor = re.search(r'<span class="field-editor"[^>]*>', html)
    assert editor is not None and re.search(r"\bhidden\b", editor.group(0))
    opener = re.search(r'<button type="button" class="value-display"[^>]*>', html)
    assert opener is not None and "data-edit-for=" in opener.group(0)
    assert 'class="revert-edit"' in html

    stylesheet = "\n".join(
        sheet.read_text(encoding="utf-8")
        for sheet in sorted((Path(app_package.__file__).parent / "static").glob("*.css"))
    )
    assert re.search(r"\.field-control \[hidden\]\s*\{[^}]*display:\s*none", stylesheet)


def test_the_closed_field_displays_exactly_what_it_will_submit(tmp_path, monkeypatch):
    # The read-only display, the control's own value and `data-prefill` are all the same
    # text, so a field nobody opened submits the value the reviewer was shown — which
    # settles `approve`.
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    card = _first_card(html)
    shown = re.search(r'<span class="current-value">(.*?)</span>', card, re.DOTALL)
    assert shown is not None and shown.group(1).strip() == "1"
    assert 'data-prefill="1"' in card
    assert _find_input_value(html, "human_score") == "1"


def _empty_string_load_stage(project_dir):
    csv_path = project_dir / "data" / "rows.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("id,flag\ne,true\nn,false\n", encoding="utf-8")
    return {"id": "load", "description": "Load rows", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}},
            "signature": {
                "form": "replaces",
                "produces": [
                    {"name": "id", "type": "str", "nullable": True},
                    {"name": "flag", "type": "bool", "nullable": True},
                ],
            }}


_EMPTY_STRING_COLUMNS = [
    {"name": "id", "type": "str", "nullable": True},
    {"name": "flag", "type": "bool", "nullable": True},
    {"name": "note", "type": "str", "nullable": True},
]


def _empty_string_row_function_stage():
    # One row holds a real empty string and one holds a null, in the same `str` column —
    # the pair a null-flattening display prints alike. A CSV cannot carry the distinction
    # (pandas reads a quoted empty field as NaN), so the frame is built by a row function.
    code = ("def transform(row):\n"
            "    return {'id': row['id'], 'flag': row['flag'],\n"
            "            'note': '' if row['id'] == 'e' else None}")
    return {"id": "note", "description": "Add notes", "type": "python_row_function",
            "inputs": [{"id": "load", "schema": {
                "columns": _EMPTY_STRING_COLUMNS[:2]}}],
            "function": {"kind": "inline", "code": code},
            "signature": {
                "form": "extends",
                "reads": [
                    {
                        "input": "load",
                        "columns": [
                            {"name": "id", "type": "str", "nullable": True},
                            {"name": "flag", "type": "bool", "nullable": True},
                        ],
                    },
                ],
                "adds": [{"name": "note", "type": "str", "nullable": True}],
            }}


def _empty_string_review_stage():
    return _with_queue_signature({
        "id": "review", "description": "Review notes", "type": "human_review_queue",
        "inputs": [{"id": "note", "schema": {
            "columns": _EMPTY_STRING_COLUMNS}}],
        "queue": {**queue_columns(source="flag", target="human_flag")}})


def test_an_empty_string_cell_is_not_printed_as_a_null(tmp_path, monkeypatch):
    # `display_cell` flattens a null to "", so a column holding a real empty string would
    # otherwise be shown as holding nothing — stating something the data does not say.
    project = "queue_route_empty_string"
    project_dir = tmp_path / project
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_empty_string_load_stage(project_dir), _empty_string_row_function_stage(),
         _empty_string_review_stage()],
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    cells = re.findall(r'<td class="kv-value">\s*(.*?)\s*</td>', html, re.DOTALL)
    assert "<em>empty text</em>" in cells
    assert "<em>no value</em>" in cells


# ── 10. Values a display must not flatten, and the empty context table ──────


def _every_column_reviewed_stage():
    # A queue over a frame whose ONLY column is the one under review, so subtracting the
    # reviewed columns leaves no context at all.
    return _with_queue_signature({
        "id": "review", "description": "Review scores", "type": "human_review_queue",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "score", "type": "int", "nullable": True}]}}],
        "queue": dict(QUEUE_COLUMNS)})


def test_no_context_table_is_rendered_when_every_column_is_under_review(tmp_path, monkeypatch):
    """No empty table, and no note inventing an explanation for its absence."""
    project = "queue_route_no_context"
    project_dir = tmp_path / project
    csv_path = project_dir / "data" / "scores.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("score\n1\n2\n", encoding="utf-8")
    load = {"id": "load", "description": "Load scores", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}},
            "signature": {
                "form": "replaces",
                "produces": [{"name": "score", "type": "int", "nullable": True}],
            }}
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project, [load, _every_column_reviewed_stage()])

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    assert '<table class="kv">' not in html
    assert "no columns" not in html
    assert 'data-target="human_score"' in html


# ── 11. A decided card is not asking for input ──────────────────────────────


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
    # `disabled` on the `<button>` itself, so a keyboard user cannot tab into and activate
    # it — a CSS-only look would leave the control live. And both halves of the hidden
    # Submit: the attribute AND the stylesheet rule, without which `.btn`'s own `display`
    # beats the UA's [hidden] rule.
    _project_dir, _run_id, fingerprints, html = _decided_queue_html(tmp_path, monkeypatch)

    decided = _first_card(html)
    opener = re.search(r'<button type="button" class="value-display"[^>]*>', decided)
    assert opener is not None and "disabled" in opener.group(0)
    assert ">Change my review<" in decided
    submit = re.search(r'<button type="submit" class="btn primary"[^>]*>', decided)
    assert submit is not None and re.search(r"\bhidden\b", submit.group(0))
    assert "Recorded: <strong>approve</strong>" in " ".join(decided.split())

    stylesheet = "\n".join(
        sheet.read_text(encoding="utf-8")
        for sheet in sorted((Path(app_package.__file__).parent / "static").glob("*.css"))
    )
    assert re.search(r"\.decision-controls \[hidden\]\s*\{[^}]*display:\s*none", stylesheet)

    # The still-undecided row is the control: live openers, primary Submit, no CTA.
    undecided = html[html.index(
        f'data-input-fingerprint="{fingerprints["input_fingerprints"][1]}"'):]
    undecided = undecided[:undecided.index("</article>")]
    live = re.search(r'<button type="button" class="value-display"[^>]*>', undecided)
    assert live is not None and "disabled" not in live.group(0)
    assert ">Change my review<" not in undecided
    open_submit = re.search(r'<button type="submit" class="btn primary"[^>]*>', undecided)
    assert open_submit is not None and not re.search(r"hidden", open_submit.group(0))


def test_unlocking_a_decided_card_records_a_new_verdict_on_resubmit(tmp_path, monkeypatch):
    # "Change my review" itself records nothing — it unlocks the card. The re-submit that
    # follows still settles its verdict from the page's prefill, which on a decided row is
    # the value the reviewer recorded before.
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
