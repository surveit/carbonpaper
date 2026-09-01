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
from app.models.records.queue_fingerprints import QueueFingerprints
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stages import llm_transform as lt
from app.services import review, versioning
from app.core.stage_cache import StageCacheEntry
from app.services.project import save_working_copy_as_version
from app.models import WorkflowStage, parse_stage
from app.models.stages.human_review_queue import ReviewVerdict
from conftest import (
    QUEUE_COLUMNS, pinned_stages, place_stage, queue_added_columns, queue_columns,
    reads_of, resumed_stages,
)
from stage_seed import add_stage

PROJECT = "queue_route_journey"


def _seed_version(root):
    vid = save_working_copy_as_version(root.name, message="test seed", reviewer="test").version_id
    versioning.publish_version(root.name, vid, reviewer="human")


def _with_queue_signature(stage, input_columns):
    """`stage` plus the signature its `queue` block implies, for fixtures about something else."""
    by_name = {column["name"]: column for column in input_columns}
    queue = stage["queue"]
    added = [{**by_name[source], "name": target}
             for source, target in queue["reviewed_columns"].items()]
    added += [{"name": queue[field], "type": "str", "nullable": True}
              for field in ("verdict_column", "reviewer_column",
                            "reviewed_at_column", "review_notes_column")
              if queue.get(field) is not None]
    return {**stage, "signature": {
        "form": "extends",
        "reads": reads_of(stage["inputs"][0]["id"], input_columns),
        "adds": added,
    }}


def _write_stage(root, filename, stage):
    root.mkdir(parents=True, exist_ok=True)
    add_stage(root, stage)


def _load_quotes_stage(root):
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


# What _build_output_row adds on top of the frozen input row, and all the runtime keeps.
_REVIEW_COLUMNS = queue_added_columns()


def _score_stage():
    return {"id": "score", "description": "Score quotes", "type": "llm_transform",
            "inputs": [{"id": "load"}],
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
    return {"id": "review", "description": "Review scores", "type": "human_review_queue",
            "inputs": [{"id": "score"}],
            "signature": {
                "form": "extends",
                "reads": reads_of("score", [
                    {"name": "id", "type": "str", "nullable": True},
                    {"name": "quote", "type": "str", "nullable": True},
                    {"name": "score", "type": "int", "nullable": True}]),
                "adds": _REVIEW_COLUMNS},
            "queue": dict(QUEUE_COLUMNS)}


def _read_fingerprints(project: str, run_id: str, stage_id: str = "review") -> dict:
    return QueueFingerprints.load(
        QueueFingerprints.compose_id(project, run_id, stage_id)).model_dump()


def _find_stage_def(project: str, stage_id: str) -> WorkflowStage:
    workflow_stage = loading.find_workflow_stage(
        loading.load_stages(project).workflow, stage_id)
    assert workflow_stage is not None
    return workflow_stage


def _decide_data(fp, reviewed, prefilled=None, reviewer="Ada", **extra):
    if prefilled is None:
        prefilled = {} if isinstance(reviewed, str) else reviewed
    return {
        "input_fingerprint": fp, "reviewer": reviewer,
        "reviewed_values": reviewed if isinstance(reviewed, str) else json.dumps(reviewed),
        "prefilled_values": prefilled if isinstance(prefilled, str) else json.dumps(prefilled),
        **extra,
    }


def _build_and_halt(tmp_path, monkeypatch):
    # The returned `input_fingerprints` are POSITIONALLY aligned to the snapshot's rows.
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(
        lt, "call_llm", lambda stage_id, llm_config, row, **kw: {"score": 1}
    )

    project_dir = tmp_path / PROJECT
    _write_stage(project_dir, "01_load.json", _load_quotes_stage(project_dir))
    _write_stage(project_dir, "02_score.json", _score_stage())
    _write_stage(project_dir, "03_review.json", _review_stage())
    _seed_version(project_dir)

    manifest = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))
    assert manifest["status"] == "awaiting_review"
    assert manifest["halted_at"] == ["review"]

    run_dir = project_dir / "runs" / manifest["run_id"]
    snapshot = pd.read_parquet(run_dir / "queue" / "review.parquet")
    fingerprints = _read_fingerprints(PROJECT, run_dir.name)
    return project_dir, manifest["run_id"], run_dir, snapshot, fingerprints


def _put_cached_decision(
    project: str, stage_id: str,
    stage_fingerprint: str, input_fingerprint: str, row: pd.Series,
    decision: ReviewVerdict, reviewed_score: float | None = None,
) -> None:
    review.record_decision(
        project_id=project, stage=place_stage(parse_stage(_review_stage())),
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
    assert "<strong>approved</strong>" in html
    # reviewed_count/total: exactly one of two rows has a prior decision.
    assert '<strong id="reviewed-count">1</strong> of <strong>2</strong> reviewed' in html
    # One field per declared reviewed column, typed from the declared column and
    # pre-filled with the value the reviewer is being asked to confirm or change.
    assert 'data-target="human_score"' in html
    assert 'type="number"' in html
    assert 'data-prefill="1"' in html  # the mocked upstream score
    # One CTA per card, opening on the WORD for the verdict it would record: the
    # undecided row can only approve as it stands, the decided one has nothing to post.
    assert html.count('class="btn cta') == html.count('<article class="queue-card')
    assert html.count(">✓ Approve</button>") == 1
    assert html.count(">✓ Recorded</button>") == 1
    # The notes box is labelled for what it is, and nothing ties a note to a change.
    assert "<span>Notes</span>" in html  # `review_notes` declares no description
    assert 'placeholder="Include any reasoning or citations for your decision"' in html


def test_the_stage_panel_says_what_the_pause_is_for_not_what_is_blocked(tmp_path, monkeypatch):
    """The halt is a designed pause with something to do, not an obstruction report."""
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/review/partial").text

    assert "A model proposed a judgement on <strong>" in html
    assert "the run paused here for a person to decide them" in html
    # The consequence is reframed, never hidden: nothing downstream runs until then.
    assert "Nothing downstream runs until each one is kept or changed" in html
    assert "queue is cleared" not in html


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
                    {"name": "score", "type": "int",
                     "description": "the score this row was labelled from",
                     "nullable": True},
                ],
            }}


def _e2e_review_stage():
    return {"id": "review", "description": "Review items", "type": "human_review_queue",
            "inputs": [{"id": "load"}],
            "signature": {
                "form": "extends",
                "reads": reads_of("load", [
                    {"name": "id", "type": "str", "nullable": True},
                    {"name": "score", "type": "int", "nullable": True}]),
                "adds": _REVIEW_COLUMNS},
            "queue": dict(QUEUE_COLUMNS)}


def test_e2e_decide_every_verdict_then_resume_completes(tmp_path, monkeypatch):
    project = "queue_route_e2e"
    workspace.set_projects_dir(tmp_path)

    project_dir = tmp_path / project
    _write_stage(project_dir, "01_load.json", _e2e_load_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _e2e_review_stage())
    _seed_version(project_dir)

    manifest = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))
    assert manifest["status"] == "awaiting_review"
    run_id = manifest["run_id"]

    run_dir = project_dir / "runs" / run_id
    snapshot = pd.read_parquet(run_dir / "queue" / "review.parquet")
    assert len(snapshot) == 3
    fingerprints = _read_fingerprints(project, run_dir.name)
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

    resumed = runner.resume_run(project_dir / "runs" / run_id, project_dir.name, run_id,
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
            "inputs": [{"id": "load"}],
            "queue": queue}, [
        {"name": "id", "type": "str", "nullable": True},
        {"name": "score", "type": "int", "nullable": True}])


def test_decide_400_on_notes_when_the_stage_declares_no_notes_column(tmp_path, monkeypatch):
    project = "queue_route_no_notes"
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / project
    _write_stage(project_dir, "01_load.json", _e2e_load_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _no_notes_review_stage())
    _seed_version(project_dir)

    manifest = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))
    run_id = manifest["run_id"]
    fingerprints = _read_fingerprints(project, run_id)

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
    drifted = _review_stage()
    drifted["queue"] = {**QUEUE_COLUMNS, "reviewed_columns": {"score": "checked_score"}}
    drifted["signature"] = {**drifted["signature"], "adds": [
        {"name": "checked_score", "type": "int", "nullable": True} if column["name"] == "human_score" else column
        for column in drifted["signature"]["adds"]
    ]}
    _write_stage(project_dir, "03_review.json", drifted)


def test_queue_page_states_the_drift_and_renders_no_items(tmp_path, monkeypatch):
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
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    assert 'id="reviewer-name"' in html
    container = re.search(r"<div[^>]*id=\"queue-items\"[^>]*>", html)
    assert container is not None and re.search(r"\bhidden\b", container.group(0))

    stylesheet = _stylesheet()
    # Without this rule the container's own `display: flex` beats the UA [hidden] rule
    # and the rows render anyway, leaving the gate visually inert.
    assert re.search(r"\.queue-items\[hidden\]\s*\{[^}]*display:\s*none", stylesheet)


def test_queue_page_prefills_a_decided_row_from_the_recorded_value(tmp_path, monkeypatch):
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
    # what the stage received is named beside it, since the recorded value (99)
    # departs from it
    received = re.search(
        r'<span class="received-value">received: ([^<]*)</span>', " ".join(decided.split()))
    assert received is not None and received.group(1).strip() == "1"
    # The control already shows the recorded value, so the row does not say it twice.
    assert "recorded-value" not in decided and "you recorded" not in decided


# ── 8. Controls whose value has its own spelling: bool, date/datetime, range ─


def _bool_review_stage(nullable):
    return _with_queue_signature({
        "id": "review", "description": "Review flags", "type": "human_review_queue",
        "inputs": [{"id": "load"}],
        "queue": {**queue_columns(source="flag", target="human_flag")}}, [
        {"name": "id", "type": "str", "nullable": True},
        {"name": "flag", "type": "bool", "nullable": nullable}])


def _build_and_halt_bool_queue(tmp_path, monkeypatch, project, *, ai_value, nullable=True):
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
    run_id = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))["run_id"]
    run_dir = project_dir / "runs" / run_id
    return run_id, _read_fingerprints(project, run_dir.name), pd.read_parquet(run_dir / "queue" / "review.parquet")


def _find_selected_option(html, target):
    # None means nothing pre-selected, which a browser renders as the FIRST option.
    select = re.search(r'<select[^>]*>(.*?)</select>', _field_block(html, target), re.DOTALL)
    assert select is not None, f"no select rendered for {target!r}"
    chosen = re.search(r'<option value="([^"]*)"[^>]*\bselected\b', select.group(1))
    return None if chosen is None else chosen.group(1)


def test_a_null_bool_ai_value_is_never_rendered_as_false(tmp_path, monkeypatch):
    run_id, _fingerprints, _snapshot = _build_and_halt_bool_queue(
        tmp_path, monkeypatch, "queue_route_bool_null", ai_value=None)

    html = TestClient(app).get(f"/project/queue_route_bool_null/runs/{run_id}/queue/review").text

    assert 'type="checkbox"' not in html
    # A NULLABLE bool has three options, so it stays a select — the radio pills are
    # for a two-value vocabulary that fits on one line.
    assert 'data-control="select"' in _field_block(html, "human_flag")
    # An undecided row has nothing recorded yet, so the field opens on the AI value
    # itself; the absent value is shown as absent, never as "false".
    assert "— unset —" in html
    assert _find_selected_option(html, "human_flag") == ""
    # The prefill the verdict is settled against carries the same absence.
    assert 'data-prefill=""' in _field_block(html, "human_flag")


def test_a_bool_select_opens_on_the_recorded_value_of_a_decided_row(tmp_path, monkeypatch):
    project = "queue_route_bool_recorded"
    run_id, fingerprints, snapshot = _build_and_halt_bool_queue(
        tmp_path, monkeypatch, project, ai_value=False)
    review.record_decision(
        project_id=project, stage=place_stage(parse_stage(_bool_review_stage(True))),
        stage_fingerprint=fingerprints["stage_fingerprint"],
        input_fingerprint=fingerprints["input_fingerprints"][0],
        frozen_row={"id": snapshot.iloc[0]["id"], "flag": bool(snapshot.iloc[0]["flag"])},
        verdict=ReviewVerdict.modify, reviewed_values={"human_flag": True},
        review_notes=None, reviewer="Ada", reviewed_at="2026-07-01T00:00:00",
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    assert _find_selected_option(html, "human_flag") == "true"
    # What the stage received is named beside the recorded value, because the two differ.
    received = re.search(
        r'<span class="received-value">received: ([^<]*)</span>', " ".join(html.split()))
    assert received is not None and received.group(1).strip() == "false"
    # The page spells a value the way the options do — never a python repr
    # sitting beside a select that reads `true`.
    assert "True" not in html and "False" not in html


def test_a_non_nullable_bool_renders_radios_and_opens_on_the_ai_value(tmp_path, monkeypatch):
    # Two options and nothing to be unset to, so reaching either costs ONE click.
    project = "queue_route_bool_required"
    run_id, fingerprints, _snapshot = _build_and_halt_bool_queue(
        tmp_path, monkeypatch, project, ai_value=False, nullable=False)

    client = TestClient(app)
    html = client.get(f"/project/{project}/runs/{run_id}/queue/review").text
    field = _field_block(html, "human_flag")
    assert 'data-control="radio"' in field
    assert "<select" not in field
    assert "— unset —" not in html                              # nothing to be unset to
    assert re.findall(r'<input type="radio"[^>]*\svalue="([^"]*)"', field) == ["true", "false"]
    assert _checked_radio(html, "human_flag") == "false"

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
        "inputs": [{"id": "load"}],
        "queue": {**queue_columns(source="seen_at", target="human_seen_at")}}, [
        {"name": "id", "type": "str", "nullable": True},
        {"name": "seen_at", "type": column_type, "nullable": True}])


def _halt_a_temporal_queue(tmp_path, project, column_type):
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
    run_id = run_prepared(prepare_run(
        project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))["run_id"]
    return run_id, _read_fingerprints(project, run_id)


def _decide_a_temporal_row(tmp_path, monkeypatch, project, column_type, recorded):
    run_id, fingerprints = _halt_a_temporal_queue(tmp_path, project, column_type)

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


# `data-target` names the stored column on the FIELD, not on the control inside it:
# a radio field holds one control per option and none of them is the field.
def _field_block(html, target):
    field = re.search(
        rf'<div class="reviewed-field"[^>]*data-target="{target}"[^>]*>.*?</div>',
        html, re.DOTALL,
    )
    assert field is not None, f"no reviewed field rendered for {target!r}"
    return field.group(0)


def _find_input_value(html, target):
    control = re.search(r'<input[^>]*class="field-control"[^>]*>', _field_block(html, target))
    assert control is not None, f"no input rendered for {target!r}"
    # `\bvalue=` would match inside another attribute ending in `value`.
    value = re.search(r'\svalue="([^"]*)"', control.group(0))
    return None if value is None else value.group(1)


def _checked_radio(html, target):
    """The value the radio group opens on, or None where no option is checked."""
    checked = re.search(r'<input type="radio"[^>]*\schecked[^>]*>', _field_block(html, target))
    if checked is None:
        return None
    return re.search(r'\svalue="([^"]*)"', checked.group(0)).group(1)


@pytest.mark.parametrize(
    "column_type, control, recorded",
    [("datetime", "datetime-local", "2026-03-04T09:30:00"), ("date", "date", "2026-03-04")],
)
def test_a_temporal_control_opens_on_the_recorded_value_of_a_decided_row(
    tmp_path, monkeypatch, column_type, control, recorded
):
    # The cache stringifies it space-separated, which the control rejects and renders BLANK.
    html = _decide_a_temporal_row(
        tmp_path, monkeypatch, f"queue_route_{column_type}", column_type, recorded)

    assert f'type="{control}"' in html
    assert _find_input_value(html, "human_seen_at") == recorded


def test_a_date_field_approved_as_it_arrived_strikes_no_received_value(tmp_path, monkeypatch):
    # A `date` control spells the received midnight shorter; the record still says approve.
    project = "queue_route_date_approved"
    run_id, fingerprints = _halt_a_temporal_queue(tmp_path, project, "date")
    client = TestClient(app)
    url = f"/project/{project}/runs/{run_id}/queue/review"

    # Approve submits exactly what the control opened on, which is what the row received.
    opened = _find_input_value(client.get(url).text, "human_seen_at")
    approved = client.post(f"{url}/decide", data=_decide_data(
        fingerprints["input_fingerprints"][0], {"human_seen_at": opened},
        prefilled={"human_seen_at": opened}))
    assert approved.status_code == 200, approved.text
    assert approved.json()["verdict"] == "approve"

    card = _first_card(client.get(url).text)
    assert "decided-approve" in card
    # Nothing departs, so the card names no received value: the live control is
    # already showing it.
    assert '<span class="received-value">' not in card


def _declared_range_review_stage():
    return {"id": "review", "description": "Review items", "type": "human_review_queue",
            "inputs": [{"id": "load"}],
            "signature": {
                "form": "extends",
                "reads": reads_of("load", [
                    {"name": "id", "type": "str", "nullable": True},
                    {"name": "score", "type": "int", "nullable": False, "range": [0, 5]}]),
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
    run_id = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))["run_id"]
    return run_id, _read_fingerprints(project, run_id)


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
    manifest = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))
    assert manifest["status"] == "awaiting_review", manifest
    run_id = manifest["run_id"]
    return run_id, _read_fingerprints(project, run_id)


def _lineage_urls(project, run_id, stage_id="review"):
    stage_def = _find_stage_def(project, stage_id)
    fingerprints = loading.load_queue_fingerprints(project, run_id, stage_id)
    assert fingerprints is not None
    return queue_view.build_lineage_urls(
        project, run_id, queue_view.resolve_lineage(stage_def, fingerprints), fingerprints
    )


def test_a_queue_directly_on_input_data_renders_and_links_to_that_stage(tmp_path, monkeypatch):
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
    code = (
        "def transform(row):\n"
        "    return {'id': row['id'], 'score': row['score'],\n"
        "            'label': 'high' if row['score'] > 1 else 'low'}"
    )
    return {"id": "label", "description": "Label items", "type": "python_row_function",
            "inputs": [{"id": "load"}],
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
                "adds": [{"name": "label", "type": "str",
                          "description": "high when the score exceeds one",
                          "nullable": True}],
            }}


def _review_labels_stage():
    return _with_queue_signature({
        "id": "review", "description": "Review labels", "type": "human_review_queue",
        "inputs": [{"id": "label"}],
        "queue": {**queue_columns(source="label", target="human_label")}}, [
        {"name": "id", "type": "str", "nullable": True},
        {"name": "score", "type": "int", "nullable": True},
        {"name": "label", "type": "str", "nullable": True}])


def test_a_queue_whose_upstream_is_not_an_llm_transform_renders_and_links(tmp_path, monkeypatch):
    project = "queue_route_on_row_function"
    project_dir = tmp_path / project
    run_id, fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _labelled_row_function_stage(), _review_labels_stage()],
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text
    for fp in fingerprints["input_fingerprints"]:
        assert f'data-input-fingerprint="{fp}"' in html
    # The shell offers an AI chat on every page, so this reads <main> alone: what is under
    # test is the queue's own markup, which must not present a row function's value as
    # something a model suggested. The two assertions either side anchor the region.
    queue = html.split("<main>", 1)[1].split("</main>", 1)[0]
    assert 'data-target="human_label"' in queue
    assert "AI" not in queue
    assert 'class="field-control"' in queue

    assert _lineage_urls(project, run_id) == [
        f"/project/{project}/runs/{run_id}/stage/label/row/{o}/trace/view"
        for o in fingerprints["row_ordinals"]
    ]


def _described_review_stage(context_columns=None):
    return {"id": "review", "description": "Review labels", "type": "human_review_queue",
            "inputs": [{"id": "label"}],
            "signature": {
                "form": "extends",
                "reads": reads_of("label", [
                    {"name": "id", "type": "str", "nullable": True},
                    {"name": "score", "type": "int",
                     "description": "the score this row was labelled from", "nullable": True},
                    {"name": "label", "type": "str",
                     "description": "high when the score exceeds one", "nullable": True}]),
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
            "queue": {
                **queue_columns(source="label", target="human_label"),
                **({} if context_columns is None else {"context_columns": context_columns}),
                "reviewer_instructions": "Confirm the label against the score.",
            }}


def _described_queue_html(tmp_path, monkeypatch, project, context_columns=None):
    project_dir = tmp_path / project
    run_id, fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _labelled_row_function_stage(),
         _described_review_stage(context_columns)],
    )
    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text
    return run_id, fingerprints, html


def _first_card(html):
    card = html[html.index('<article class="queue-card'):]
    return card[:card.index("</article>")]


def _stylesheet():
    return "\n".join(
        sheet.read_text(encoding="utf-8")
        for sheet in sorted((Path(app_package.__file__).parent / "static").glob("*.css"))
    )


def test_the_card_renders_the_described_queued_row_and_its_review_section(tmp_path, monkeypatch):
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

    # A declared description hangs off a help marker beside the column name — the
    # marker is what tells a reviewer there is anything to hover. `id` declares no
    # description and gets no marker.
    described = re.search(
        r'<code>(\w+)</code><span class="kv-help"[^>]*data-tip="([^"]*)"', card)
    assert described is not None
    assert described.groups() == ("score", "the score this row was labelled from")
    assert re.search(r'<code>id</code>\s*</th>', table) is not None

    # A reviewed field is labelled with the column it reviews — `label`, the column
    # the value arrived in, never the `human_label` the answer is stored in — and
    # carries that source column's declared description as its tooltip.
    reviewed = re.search(
        r'<div class="reviewed-field"[^>]*>\s*<label([^>]*)>([^<]*)</label>', card)
    assert reviewed is not None
    assert 'title="high when the score exceeds one"' in reviewed.group(1)
    assert reviewed.group(2).strip() == "label"

    positions = re.findall(r'<span class="row-position">([^<]*)</span>', html)
    assert positions == [f"Row {n} of {len(positions)}" for n in range(1, len(positions) + 1)]
    assert "identity-cell" not in html
    assert (f'href="/project/queue_route_card/runs/{run_id}/stage/label/row/'
            f'{fingerprints["row_ordinals"][0]}/trace/view"') in card

    # The reviewer brief is their brief, not the raw stage handle's monospace.
    assert '<pre class="instructions">' not in html
    assert '<p class="instructions-text">Confirm the label against the score.</p>' in html


def test_the_unreviewed_columns_are_labelled_as_what_the_review_is_judged_against(
        tmp_path, monkeypatch):
    _run_id, _fingerprints, html = _described_queue_html(
        tmp_path, monkeypatch, "queue_route_context_label")

    card = _first_card(html)
    block = card[card.index('<div class="queued-row"'):card.index("Columns to review")]
    # These columns are not up for edit, but they ARE what the reviewer weighs the
    # edit against — so the label must not read as "ignore this", and it carries the
    # same heading as the columns below it rather than folding away behind a summary.
    assert '<p class="review-section-heading">Context for review</p>' in block
    assert "not under review" not in card
    assert "<details" not in card


def test_the_card_shows_only_declared_context_columns(tmp_path, monkeypatch):
    _run_id, _fingerprints, html = _described_queue_html(
        tmp_path, monkeypatch, "queue_route_selected_context", ["score"])

    card = _first_card(html)
    table = card[card.index('<table class="kv">'):card.index("</table>")]

    assert re.findall(r"<code>(\w+)</code>", table) == ["score"]
    assert "label" in card


def test_the_field_rows_render_under_one_heading_with_a_live_control(tmp_path, monkeypatch):
    # One column per field: the reviewer edits it where the value is shown.
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    card = _first_card(html)
    assert re.findall(r'<p class="field-column-heading[^"]*">([^<]*)</p>', card) == [
        "Columns to review"]
    assert "Your review" not in card
    field = re.search(r'<div class="reviewed-field" data-dirty="false"[^>]*>', card)
    assert field is not None
    # The control is live from the first paint — there is no state to leave before
    # the value can be edited, and so no per-field Approve to press before Submit.
    assert re.search(r'<input class="field-control"[^>]*>', card)
    for gone in ("data-field-approve", "data-field-edit", "data-field-save",
                 "data-field-cancel", "data-unlock"):
        assert gone not in card, gone
    # Presence-only attribute — the interface names it bracket-style, because it
    # carries no value.
    assert "data-field-revert" in card


def test_only_an_edited_field_offers_the_way_back(tmp_path, monkeypatch):
    # A Revert over an unedited field is a control that never does anything.
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text
    card = _first_card(html)
    revert = re.search(r'<span class="field-revert">', card)
    assert revert is not None
    # The prefill is rendered struck through, ready for the edit that reveals it.
    assert re.search(r'<span class="prefill-was">was <s>', card)

    stylesheet = _stylesheet()
    assert re.search(
        r'\.reviewed-field\[data-dirty="false"\] \.field-revert[^{]*\{[^}]*display:\s*none',
        stylesheet,
    )


def test_an_edited_field_is_marked_on_the_edge_the_eye_scans(tmp_path, monkeypatch):
    # The rail is the widest edge, so an edited field survives a fast scroll.
    stylesheet = _stylesheet()
    rule = re.search(r'\.reviewed-field\[data-dirty="true"\]\s*\{([^}]*)\}', stylesheet)
    assert rule is not None
    assert "border-left-color: var(--warn-bd)" in rule.group(1)
    assert "background" in rule.group(1)


def test_a_two_value_vocabulary_renders_pills_that_can_still_be_reached_by_keyboard():
    # Off-screen, never `display: none`: that drops the group out of the tab order.
    stylesheet = _stylesheet()
    rule = re.search(r'\.radio-group input\s*\{([^}]*)\}', stylesheet)
    assert rule is not None
    assert "clip-path" in rule.group(1)
    assert not re.search(r"display:\s*none", rule.group(1))
    checked = re.search(r'\.radio-group input:checked \+ label\s*\{([^}]*)\}', stylesheet)
    assert checked is not None and "var(--accent)" in checked.group(1)
    focused = re.search(r'\.radio-group input:focus-visible \+ label\s*\{([^}]*)\}', stylesheet)
    assert focused is not None and "var(--focus)" in focused.group(1)


# ── A str column opens a <textarea> once ITS QUEUE'S values run long ────────


def _long_note_load_stage(project_dir):
    (project_dir / "data").mkdir(parents=True, exist_ok=True)
    csv_path = project_dir / "data" / "notes.csv"
    long_note = "This alignment note repeats until it passes the multiline threshold. " * 3
    pd.DataFrame({"id": ["a", "b"], "note": [long_note, "short"]}).to_csv(csv_path, index=False)
    return {"id": "load", "description": "Load notes", "type": "input_data",
            "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}},
            "signature": {"form": "replaces", "produces": [
                {"name": "id", "type": "str", "nullable": True},
                {"name": "note", "type": "str", "nullable": True},
            ]}}


def _long_note_review_stage():
    return _with_queue_signature({
        "id": "review", "description": "Review notes", "type": "human_review_queue",
        "inputs": [{"id": "load"}],
        "queue": {**queue_columns(source="note", target="human_note")}}, [
        {"name": "id", "type": "str", "nullable": True},
        {"name": "note", "type": "str", "nullable": True}])


def test_a_column_whose_queue_values_run_long_renders_a_textarea(tmp_path, monkeypatch):
    project = "queue_route_multiline"
    project_dir = tmp_path / project
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_long_note_load_stage(project_dir), _long_note_review_stage()],
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    assert 'data-control="textarea"' in _field_block(html, "human_note")
    assert re.search(r'<textarea class="field-control"', html)
    # One SOURCE column, one control choice for the whole queue: the row whose
    # own value ("short") is short still opens the same textarea as row "a".
    assert html.count('data-target="human_note"') == 2


def test_a_short_str_column_still_renders_an_input(tmp_path, monkeypatch):
    project = "queue_route_short_str_column"
    project_dir = tmp_path / project
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_e2e_load_stage(project_dir), _labelled_row_function_stage(), _review_labels_stage()],
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    field = _field_block(html, "human_label")
    assert 'data-control="text"' in field
    control = re.search(r'<input class="field-control"[^>]*>', field)
    assert control is not None and 'type="text"' in control.group(0)


def test_the_recorded_line_is_where_a_decided_card_names_the_stored_column(
        tmp_path, monkeypatch):
    _project_dir, _run_id, _fingerprints, html = _decided_queue_html(tmp_path, monkeypatch)

    decided = " ".join(_first_card(html).split())
    recorded = decided[decided.index('<p class="prior-decision">'):]
    assert "human_score" in recorded
    fields = decided[decided.index('<div class="reviewed-fields">'):
                     decided.index('<div class="decision-controls">')]
    assert "human_score" not in re.sub(r"<[^>]*>", " ", fields)


def test_an_undecided_card_opens_ready_to_approve_as_it_stands(tmp_path, monkeypatch):
    # Nothing gates it: every field already holds the value the CTA would record.
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    card = _first_card(html)
    cta = re.search(r'<button type="submit" class="btn cta approve"[^>]*>([^<]*)</button>', card)
    assert cta is not None and "disabled" not in cta.group(0)
    assert cta.group(1) == "✓ Approve"
    gate = re.search(r'<span class="decide-gate"[^>]*>([^<]*)</span>', card)
    assert gate is not None and gate.group(1).strip() == "1 column · nothing changed"


def test_the_field_displays_exactly_what_it_will_submit(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    card = _first_card(html)
    # The control's value IS the display, so the two cannot disagree — and the
    # prefill the verdict is settled against is the same value again.
    assert _find_input_value(html, "human_score") == "1"
    assert 'data-prefill="1"' in card


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
    # A CSV cannot carry this: pandas reads a quoted empty field as NaN, hence the code.
    code = ("def transform(row):\n"
            "    return {'id': row['id'], 'flag': row['flag'],\n"
            "            'note': '' if row['id'] == 'e' else None}")
    return {"id": "note", "description": "Add notes", "type": "python_row_function",
            "inputs": [{"id": "load"}],
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
        "inputs": [{"id": "note"}],
        "queue": {**queue_columns(source="flag", target="human_flag")}}, [
        {"name": "id", "type": "str", "nullable": True},
        {"name": "flag", "type": "bool", "nullable": True},
        {"name": "note", "type": "str", "nullable": True}])


def test_an_empty_string_cell_is_not_printed_as_a_null(tmp_path, monkeypatch):
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


def _addressed_row_function_stage():
    code = ("def transform(row):\n"
            "    return {'id': row['id'], 'flag': row['flag'],\n"
            "            'note': 'https://example.org/mill-list.pdf'\n"
            "                    if row['id'] == 'e' else 'see the filing'}")
    return {"id": "note", "description": "Add notes", "type": "python_row_function",
            "inputs": [{"id": "load"}],
            "function": {"kind": "inline", "code": code},
            "signature": {
                "form": "extends",
                "reads": [{"input": "load", "columns": [
                    {"name": "id", "type": "str", "nullable": True},
                    {"name": "flag", "type": "bool", "nullable": True},
                ]}],
                "adds": [{"name": "note", "type": "str", "nullable": True}],
            }}


def test_a_cell_holding_an_address_is_rendered_as_a_link(tmp_path, monkeypatch):
    """A reviewer decides against the sources a row names; one they cannot open is not one."""
    project = "queue_route_addressed_cell"
    project_dir = tmp_path / project
    run_id, _fingerprints = _build_and_halt_queue_over(
        tmp_path, monkeypatch, project,
        [_empty_string_load_stage(project_dir), _addressed_row_function_stage(),
         _empty_string_review_stage()],
    )

    html = TestClient(app).get(f"/project/{project}/runs/{run_id}/queue/review").text

    cells = re.findall(r'<td class="kv-value">\s*(.*?)\s*</td>', html, re.DOTALL)
    linked = [cell for cell in cells if cell.startswith("<a ")]
    assert linked == ['<a href="https://example.org/mill-list.pdf" target="_blank" '
                      'rel="noopener noreferrer">https://example.org/mill-list.pdf</a>']
    # Prose that merely mentions a source stays prose: a link is only offered where
    # the whole cell IS the address, so nothing invents one out of a sentence.
    assert "see the filing" in cells


# ── 10. Values a display must not flatten, and the empty context table ──────


def _every_column_reviewed_stage():
    return _with_queue_signature({
        "id": "review", "description": "Review scores", "type": "human_review_queue",
        "inputs": [{"id": "load"}],
        "queue": dict(QUEUE_COLUMNS)}, [
        {"name": "score", "type": "int", "nullable": True}])


def test_no_context_table_is_rendered_when_every_column_is_under_review(tmp_path, monkeypatch):
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
    project_dir, run_id, _run_dir, snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    _put_cached_decision(
        PROJECT, "review", fingerprints["stage_fingerprint"],
        fingerprints["input_fingerprints"][0], snapshot.iloc[0], ReviewVerdict.approve,
    )
    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text
    return project_dir, run_id, fingerprints, html


def test_a_decided_card_keeps_its_fields_live_and_its_cta_dead(tmp_path, monkeypatch):
    # No unlock step: the CTA is dead only until something differs.
    _project_dir, _run_id, fingerprints, html = _decided_queue_html(tmp_path, monkeypatch)

    decided = _first_card(html)
    assert re.search(r'<div class="reviewed-field" data-dirty="false"[^>]*>', decided)
    assert re.search(r'<input class="field-control"[^>]*>', decided)
    assert ">Change my review<" not in decided
    cta = re.search(r'<button type="submit"[^>]*>([^<]*)</button>', decided)
    assert cta is not None and "disabled" in cta.group(0)
    assert cta.group(1) == "✓ Recorded"

    undecided = _undecided_card(html, fingerprints)
    live = re.search(r'<button type="submit"[^>]*>([^<]*)</button>', undecided)
    assert live is not None and "disabled" not in live.group(0)
    assert live.group(1) == "✓ Approve"


def _undecided_card(html, fingerprints):
    card = html[html.index(f'data-input-fingerprint="{fingerprints["input_fingerprints"][1]}"'):]
    return card[:card.index("</article>")]


def test_a_decided_field_names_what_the_stage_produced_only_where_the_record_departs(
    tmp_path, monkeypatch
):
    # Where the record kept it, the live control already shows it; twice is noise.
    _project_dir, run_id, _run_dir, _snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    client = TestClient(app)
    modified = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fingerprints["input_fingerprints"][0],
                          {"human_score": "7"}, prefilled={"human_score": "1"}),
    )
    assert modified.status_code == 200, modified.text
    # `call_llm` is mocked to always answer {"score": 1}, so approving exactly what
    # the row received is recording "1" — the one value that leaves nothing to name.
    approved = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fingerprints["input_fingerprints"][1],
                          {"human_score": "1"}, prefilled={"human_score": "1"}),
    )
    assert approved.status_code == 200, approved.text

    html = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text

    modified_card = _first_card(html)
    received = re.search(
        r'<span class="received-value">received: ([^<]*)</span>', " ".join(modified_card.split()))
    assert received is not None and received.group(1).strip() == "1"
    # The second card by fingerprint, decided since the POST above: without this the
    # negative below would pass on a card that carries no decision at all.
    approved_card = _undecided_card(html, fingerprints)
    assert '<span class="verdict-chip verdict-approve">' in approved_card
    assert '<span class="received-value">' not in approved_card


def test_a_field_says_which_column_it_decides_and_the_cta_says_what_it_would_record(
    tmp_path, monkeypatch
):
    # Nothing else on the card names the column or the verdict, so these must.
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text
    card = _first_card(html)

    assert 'aria-label="Revert score"' in card
    revert = re.search(r"<button[^>]*data-field-revert[^>]*>", card)
    # It puts a value back into the control, so it says which control.
    assert revert is not None and "aria-controls" in revert.group(0)

    gate = re.search(r'<span class="decide-gate"[^>]*>([^<]*)</span>', card)
    assert gate is not None and 'role="status"' in gate.group(0)
    assert gate.group(1).strip() == "1 column · nothing changed"
    gate_id = re.search(r'id="([^"]*)"', gate.group(0))
    assert gate_id is not None and html.count(f'id="{gate_id.group(1)}"') == 1
    cta = re.search(r'<button type="submit"[^>]*>', card)
    assert cta is not None and f'aria-describedby="{gate_id.group(1)}"' in cta.group(0)


def test_a_decided_card_states_its_verdict_in_a_word(tmp_path, monkeypatch):
    _project_dir, _run_id, fingerprints, html = _decided_queue_html(tmp_path, monkeypatch)

    chip = re.search(r'<span class="verdict-chip verdict-approve">([^<]*)</span>',
                     _first_card(html))
    # Past tense: the chip names a decision already taken, not one on offer. The
    # class keeps the stored value, so a substring check here would pass either way.
    assert chip is not None and chip.group(1).strip() == "✓ approved"
    assert "verdict-chip" not in _undecided_card(html, fingerprints)

    stylesheet = _stylesheet()
    # The body keeps --raised: tinting it is what made the card's own white controls
    # read as holes punched in it. The rail and the chip carry the verdict instead.
    for verdict, stroke in (("approve", "--good-bd"), ("modify", "--warn-bd")):
        rail = re.search(rf"\.queue-card\.decided-{verdict}\s*\{{([^}}]*)\}}", stylesheet)
        assert rail is not None and f"border-left-color: var({stroke})" in rail.group(1)
        assert "background" not in rail.group(1)
        chip_rule = re.search(rf"\.verdict-chip\.verdict-{verdict}\s*\{{([^}}]*)\}}", stylesheet)
        assert chip_rule is not None and f"border-color: var({stroke})" in chip_rule.group(1)


def test_a_decided_cards_note_stays_live_like_its_fields(tmp_path, monkeypatch):
    # The CTA re-records the note, so freezing it would bar the commonest correction.
    _project_dir, _run_id, fingerprints, html = _decided_queue_html(tmp_path, monkeypatch)

    decided = re.search(r"<textarea[^>]*>", _first_card(html))
    assert decided is not None
    assert "readonly" not in decided.group(0) and "disabled" not in decided.group(0)

    live = re.search(r"<textarea[^>]*>", _undecided_card(html, fingerprints))
    assert live is not None and "readonly" not in live.group(0)

    # A decision must not COLLAPSE the box. A decided card keeps the space an
    # undecided one gave it, because a decision that reflowed the page under the
    # reviewer is the defect this page was rebuilt to remove.
    assert not re.search(r"\.review-notes[^{]*:placeholder-shown[^{]*\{[^}]*display:\s*none",
                         _stylesheet())


def test_recording_a_decision_cannot_move_the_page_under_the_reviewer():
    stylesheet = _stylesheet()

    # Measured: without these two, submitting moved the controls 39px and scrolled
    # the page 42px. A decided card grows BELOW the button just pressed, and both
    # rules are what keep that growth from reaching anything above it.
    assert re.search(r"\.queue-items\s*\{[^}]*overflow-anchor:\s*none", stylesheet)
    assert re.search(r"\.queue-card header\s*\{[^}]*min-height:", stylesheet)


def test_a_recorded_note_is_what_the_card_reopens_on_and_what_it_reposts(tmp_path, monkeypatch):
    _project_dir, run_id, fingerprints, _html = _decided_queue_html(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]
    client = TestClient(app)
    posted = client.post(
        f"/project/{PROJECT}/runs/{run_id}/queue/review/decide",
        data=_decide_data(fp, {"human_score": "1"}, review_notes="the note as recorded"))
    assert posted.status_code == 200, posted.text

    card = _first_card(
        client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review/card/{fp}").text)
    reopened = re.search(r"<textarea([^>]*)>(.*?)</textarea>", card, re.DOTALL)
    # Live, and carrying the note: editing it is what re-arms the CTA on this card.
    assert reopened is not None and "readonly" not in reopened.group(1)
    assert reopened.group(2) == "the note as recorded"

    source = (Path(app_package.__file__).parent / "templates" / "queue.html").read_text(
        encoding="utf-8")
    # And the payload is assembled field by field off the elements, so nothing that
    # governs form serialisation stands between that value and the post.
    assert re.search(
        r"const notes = card\.querySelector\('\[data-role=\"notes\"\]'\);\s*"
        r"if \(notes\) fd\.append\('review_notes', notes\.value\);", source)
    assert "new FormData(form" not in source
    # And the note is inside the snapshot a decided card is compared against, so
    # changing it alone is enough to bring the CTA back to life.
    assert re.search(
        r"function snapshotCard\(card\) \{[^}]*notes \? notes\.value : null", source, re.DOTALL)


def test_editing_a_decided_card_records_a_new_verdict_on_resubmit(tmp_path, monkeypatch):
    _project_dir, run_id, fingerprints, html = _decided_queue_html(tmp_path, monkeypatch)

    decided = _first_card(html)
    assert 'data-prefill="1"' in decided  # the recorded value the card opens on
    # No unlock to press first: the field is live and the verdict is settled against
    # that prefill, so a new value posts straight through as `modify`.
    assert "data-unlock" not in decided

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


# ── 12. Re-rendering one card, and the Resume run form ──────────────────────


def test_the_card_route_re_renders_the_decided_state_of_one_row(tmp_path, monkeypatch):
    _project_dir, run_id, fingerprints, html = _decided_queue_html(tmp_path, monkeypatch)
    fp = fingerprints["input_fingerprints"][0]

    r = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review/card/{fp}")

    assert r.status_code == 200
    card = _first_card(r.text)
    assert f'data-input-fingerprint="{fp}"' in card
    assert "Recorded: <strong>approved</strong>" in " ".join(card.split())
    assert ">✓ Recorded</button>" in card
    # The page loops over the same partial, so the swapped-in card is the card
    # the page would have rendered for that row — including its "Row 1 of 2".
    assert card == _first_card(html)
    assert "Row 1 of 2" in " ".join(card.split())
    # The recorded block sits AFTER the controls: a decision then adds nothing
    # above the button the reviewer just pressed, so the swap moves nothing they
    # are looking at. Everything the swap adds is below that point.
    assert card.index('class="decision-controls"') < card.index('class="prior-decision"')


def test_the_card_route_404s_on_a_fingerprint_this_queue_does_not_carry(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, _snapshot, _fingerprints = _build_and_halt(tmp_path, monkeypatch)

    r = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review/card/nosuchfingerprint")

    assert r.status_code == 404
    assert "nosuchfingerprint" in r.json()["detail"]


def _resume_form(html):
    return re.search(r"<form id=\"resume-run\"[^>]*>", html, re.DOTALL)


def test_the_resume_form_is_rendered_hidden_until_every_row_is_decided(tmp_path, monkeypatch):
    _project_dir, run_id, _run_dir, snapshot, fingerprints = _build_and_halt(tmp_path, monkeypatch)
    client = TestClient(app)
    url = f"/project/{PROJECT}/runs/{run_id}/queue/review"

    # The script reveals the form on the decision that completes the queue, so it
    # is in the page from the start — carrying `hidden`, and no inline `display`
    # that would outrank it.
    with_none_decided = _resume_form(client.get(url).text)
    assert with_none_decided is not None
    assert re.search(r"\bhidden\b", with_none_decided.group(0))
    assert "display" not in with_none_decided.group(0)

    for fp, (_position, row) in zip(fingerprints["input_fingerprints"], snapshot.iterrows()):
        _put_cached_decision(
            PROJECT, "review", fingerprints["stage_fingerprint"], fp, row,
            ReviewVerdict.approve,
        )

    with_all_decided = _resume_form(client.get(url).text)
    assert with_all_decided is not None
    assert not re.search(r"\bhidden\b", with_all_decided.group(0))


# ── 13. Paging the queue on the client: what the server must ship for it ────
#
# The pager itself is exercised in tests/test_queue_paginate_client.py; these
# cover the half of it the server owns — where the cards are put, and that the
# numbers on them describe the whole queue rather than a page of it.


PAGED_PROJECT = "queue_route_paged"


def _scaled_load_stage(root, rows):
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "items.csv"
    pd.DataFrame({
        "id": [f"row-{i:03d}" for i in range(rows)],
        "score": list(range(rows)),
    }).to_csv(csv_path, index=False)
    return {"id": "load", "description": "Load items", "type": "input_data",
            "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}},
            "signature": {"form": "replaces", "produces": [
                {"name": "id", "type": "str", "nullable": True},
                {"name": "score", "type": "int", "nullable": True}]}}


def _build_and_halt_a_queue_of(tmp_path, monkeypatch, rows, *, sort=None):
    review_stage = _e2e_review_stage()
    if sort is not None:
        review_stage["queue"] = {**review_stage["queue"], "sort": sort}
    return _build_and_halt_queue_over(
        tmp_path, monkeypatch, PAGED_PROJECT,
        [_scaled_load_stage(tmp_path / PAGED_PROJECT, rows), review_stage],
    )


def _queue_html(tmp_path, monkeypatch, rows, *, sort=None):
    run_id, fingerprints = _build_and_halt_a_queue_of(tmp_path, monkeypatch, rows, sort=sort)
    html = TestClient(app).get(f"/project/{PAGED_PROJECT}/runs/{run_id}/queue/review").text
    return run_id, fingerprints, html


def _template_content(html):
    body = html[html.index('<template id="queue-cards">'):]
    return body[:body.index("</template>")]


def test_the_queue_ships_every_card_inside_a_template_and_an_empty_live_list(
    tmp_path, monkeypatch
):
    """Content of a <template> is parsed but never laid out — that is the whole fix."""
    _run_id, fingerprints, html = _queue_html(tmp_path, monkeypatch, 30)

    live_list = re.search(r"<div[^>]*id=\"queue-items\"[^>]*>\s*</div>", html)
    assert live_list is not None, "the list the page opens on must be empty of cards"
    cards = _template_content(html)
    assert cards.count('<article class="queue-card') == 30
    # Every card, in the queue's review order: the client pages over this list
    # from the front, so page 1 is the first 25 rows of it.
    assert re.findall(r'data-input-fingerprint="([^"]+)"', cards) == (
        fingerprints["input_fingerprints"]
    )


def test_a_declared_review_order_decides_which_rows_land_on_page_one(tmp_path, monkeypatch):
    """queue.sort exists so the rows worth reading first are the first ones shipped."""
    _run_id, _fingerprints, html = _queue_html(
        tmp_path, monkeypatch, 30,
        sort=[{"column": "score", "direction": "descending"}],
    )

    cards = _template_content(html)
    ids = re.findall(r"<td[^>]*>(row-\d+)</td>", cards)
    assert len(ids) == 30
    assert ids[:25] == [f"row-{i:03d}" for i in range(29, 4, -1)]


def test_row_numbers_are_absolute_over_the_queue_not_over_a_page(tmp_path, monkeypatch):
    _run_id, _fingerprints, html = _queue_html(tmp_path, monkeypatch, 30)

    positions = re.findall(r'class="row-position">Row (\d+) of (\d+)<', html)
    assert positions == [(str(n), "30") for n in range(1, 31)]
    # The 26th row — first on page 2 — is numbered 26, not 1.
    assert ("26", "30") in positions


def test_the_pager_is_rendered_above_and_below_the_list_and_starts_hidden(
    tmp_path, monkeypatch
):
    _run_id, _fingerprints, html = _queue_html(tmp_path, monkeypatch, 30)

    pagers = re.findall(r"<nav class=\"queue-pager\".*?</nav>", html, re.DOTALL)
    assert len(pagers) == 2
    assert all(re.search(r"\bhidden\b", nav[:nav.index(">")]) for nav in pagers)
    # Above the list and below it, so neither end of a page is a dead end.
    list_at = html.index('id="queue-items"')
    assert html.index('class="queue-pager"') < list_at < html.rindex('class="queue-pager"')
    # Both ends of the queue keep their control, disabled rather than removed.
    for nav in pagers:
        assert 'data-page-step="-1"' in nav and 'data-page-step="1"' in nav
        assert 'class="pager-readout"' in nav

    stylesheet = _stylesheet()
    # Without this rule the nav's own `display: flex` beats the UA [hidden] rule
    # and a one-page queue is offered controls that do nothing.
    assert re.search(r"\.queue-pager\[hidden\]\s*\{[^}]*display:\s*none", stylesheet)


def test_progress_and_resume_are_seeded_from_the_whole_queue_not_a_page(tmp_path, monkeypatch):
    run_id, fingerprints, _html = _queue_html(tmp_path, monkeypatch, 30)
    snapshot = pd.read_parquet(
        tmp_path / PAGED_PROJECT / "runs" / run_id / "queue" / "review.parquet"
    )
    # Decide three rows on page 2, which the page never has in its live list at load.
    for position in (25, 26, 27):
        row = snapshot.iloc[position]
        review.record_decision(
            project_id=PAGED_PROJECT, stage=place_stage(parse_stage(_e2e_review_stage())),
            stage_fingerprint=fingerprints["stage_fingerprint"],
            input_fingerprint=fingerprints["input_fingerprints"][position],
            frozen_row={"id": row["id"], "score": int(row["score"])},
            verdict=ReviewVerdict.approve,
            reviewed_values={"human_score": int(row["score"])},
            review_notes=None, reviewer="local", reviewed_at="2026-07-01T00:00:00",
        )

    html = TestClient(app).get(f"/project/{PAGED_PROJECT}/runs/{run_id}/queue/review").text

    assert '<strong id="reviewed-count">3</strong> of <strong>30</strong> reviewed' in html
    # The counter is a seeded number, never a count of the cards on the page —
    # which is why paging cannot corrupt it.
    assert "let reviewedCount = 3;" in html
    assert ".decided" not in html.split("<script>")[-1]


def test_the_page_size_is_named_once_in_the_module_the_page_reads_it_from(tmp_path):
    source = (
        Path(app_package.__file__).parent / "templates" / "queue.html"
    ).read_text(encoding="utf-8")

    assert re.search(r"createQueuePager\([^;]*QUEUE_PAGE_SIZE", source, re.DOTALL)
    assert "/static/queue-paginate.js" in source


# ── 7. Duplicate rows: one decision, because a decision is about content ────


def _identical_quotes_stage(root):
    """The two rows are identical across every column — what the runner used to refuse."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "quotes.csv"
    pd.DataFrame({
        "id": ["a", "a"],
        "quote": ["Quote about widgets.", "Quote about widgets."],
    }).to_csv(csv_path, index=False)
    return {"id": "load", "description": "Load quotes", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}},
            "signature": {"form": "replaces", "produces": [
                {"name": "id", "type": "str", "nullable": True},
                {"name": "quote", "type": "str", "nullable": True}]}}


def test_two_identical_rows_share_one_decision_and_both_carry_it(tmp_path, monkeypatch):
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(lt, "call_llm", lambda stage_id, llm_config, row, **kw: {"score": 1})
    project_dir = tmp_path / PROJECT
    _write_stage(project_dir, "01_load.json", _identical_quotes_stage(project_dir))
    _write_stage(project_dir, "02_score.json", _score_stage())
    _write_stage(project_dir, "03_review.json", _review_stage())
    _seed_version(project_dir)

    manifest = run_prepared(
        prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))
    assert manifest["status"] == "awaiting_review"
    run_id = manifest["run_id"]
    run_dir = project_dir / "runs" / run_id
    snapshot = pd.read_parquet(run_dir / "queue" / "review.parquet")
    fingerprints = _read_fingerprints(PROJECT, run_id)

    # Both rows queue, and both address the same decision: the fingerprint is the
    # row's CONTENT, so there is one thing to decide, not two.
    assert len(snapshot) == 2
    assert len(set(fingerprints["input_fingerprints"])) == 1
    assert fingerprints["row_ordinals"] == [0, 1]

    fp = fingerprints["input_fingerprints"][0]
    _put_cached_decision(
        PROJECT, "review", fingerprints["stage_fingerprint"], fp,
        snapshot.iloc[0], ReviewVerdict.modify, reviewed_score=99,
    )

    # One decision settles the queue: the page counts the row content, not the rows.
    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/queue/review").text
    assert '<strong id="reviewed-count">2</strong> of <strong>2</strong> reviewed' in html

    resumed = runner.resume_run(project_dir / "runs" / run_id, project_dir.name, run_id,
                                *resumed_stages(project_dir, run_id))
    assert resumed["status"] == "ok"
    out = pd.read_parquet(run_dir / "outputs" / "review.parquet")
    assert len(out) == 2
    assert list(out["human_score"]) == [99, 99]
    assert list(out["decision"]) == ["modify", "modify"]
