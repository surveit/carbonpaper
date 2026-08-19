"""The spend page reads two records and states what neither of them recorded."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.agent.store import AgentSession, SessionStore
from app.core.agent.usage import LlmUsage
from app.core.persistence import get_store
from app.core.run_status import RunStatus, StageStatus
from app.main import app
from app.models.run_manifest import StageRecord
from app.models.stages.stage_base import StageType
from app.services.project import Project
from app.runtime.manifest import RunManifest
from app.web.admin.spend import (
    NO_PROJECT,
    UNRECORDED_MODEL,
    SpendSource,
    read_session_spend,
    read_workspace_spend,
)

client = TestClient(app)


def _stage_record(stage_id: str, usage: LlmUsage | None, started_at: str) -> StageRecord:
    return StageRecord(
        stage_id=stage_id, type=StageType.llm_transform, started_at=started_at,
        status=StageStatus.OK, input_validation_report=[], output_validation_report=None,
        output_row_count=1, llm_usage=usage,
    )


def _store_run(project: str, run_id: str, records: list[StageRecord], area: str = "runs") -> None:
    Project(id=project, name=project).save()
    RunManifest(
        id=RunManifest.compose_id(project, run_id, area),
        run_id=run_id, started_at="2026-08-16T09:00:00", project=project,
        workflow_version=None, human_review_queue_stats={},
        status=RunStatus.OK, stage_records=records,
    ).save()


def test_run_stages_and_chat_turns_add_up_to_one_total():
    _store_run("congresswatch", "20260816T090000", [
        _stage_record("score", LlmUsage(cost_usd=2.50, calls=4, input_tokens=100,
                                        output_tokens=20, model="claude-opus-5"),
                      "2026-08-16T09:00:00"),
        _stage_record("load", None, "2026-08-16T09:00:00"),
    ])
    store = SessionStore()
    sid = store.create(title="Edit the score stage", context={"project_id": "congresswatch"})
    store.record_turn_spend(sid, LlmUsage(cost_usd=0.25, calls=1, input_tokens=30,
                                          output_tokens=5, model="claude-sonnet-5"))

    spend = read_workspace_spend()

    assert spend.total.cost_usd == 2.75
    assert spend.total.calls == 5
    # The stage that called no model contributes no entry, so it cannot be counted
    # as a zero-cost one.
    assert spend.total.entries == 2
    assert [t.label for t in spend.by_source] == [SpendSource.run.value,
                                                  SpendSource.agent_session.value]
    assert [(t.label, t.cost_usd) for t in spend.by_project] == [("congresswatch", 2.75)]
    assert [t.label for t in spend.by_model] == ["claude-opus-5", "claude-sonnet-5"]


def test_a_session_that_recorded_no_turn_is_counted_not_zeroed():
    SessionStore().create(title="Opened, never answered")

    spend = read_workspace_spend()

    assert spend.silent_sessions == 1
    assert spend.total.entries == 0


def test_a_manifest_this_app_cannot_parse_is_counted_not_skipped_silently():
    _store_run("congresswatch", "20260816T090000",
               [_stage_record("score", LlmUsage(cost_usd=1.0, calls=1), "2026-08-16T09:00:00")])
    # A payload RunManifest rejects, written straight at the store.
    get_store().write("run", "congresswatch/runs/20260816T100000", {"nonsense": True})

    spend = read_workspace_spend()

    assert spend.unreadable_runs == 1
    assert spend.total.cost_usd == 1.0


def test_an_eval_run_is_totalled_but_carries_no_link():
    _store_run("congresswatch", "20260816T110000",
               [_stage_record("score", LlmUsage(cost_usd=0.40, calls=1), "2026-08-16T11:00:00")],
               area="eval_run")

    spend = read_workspace_spend()

    assert spend.total.cost_usd == 0.40
    assert [e.link for e in spend.biggest] == [None]
    # Nothing recorded the model, and the page says so rather than naming one.
    assert [t.label for t in spend.by_model] == [UNRECORDED_MODEL]


def test_a_chat_that_names_no_project_is_labelled_rather_than_dropped():
    store = SessionStore()
    sid = store.create(title="Just a chat")
    store.record_turn_spend(sid, LlmUsage(cost_usd=0.10, calls=1))

    entries = read_session_spend(AgentSession.list(), {})

    assert [e.project for e in entries] == [NO_PROJECT]
    assert [e.link for e in entries] == [f"/chat/{sid}"]


def test_a_run_under_a_project_the_workspace_does_not_list_is_not_read():
    """The cost of reading per project: runs outliving their project record fall out of the total."""
    _store_run("congresswatch", "20260816T090000",
               [_stage_record("score", LlmUsage(cost_usd=1.0, calls=1), "2026-08-16T09:00:00")])
    Project.delete("congresswatch")

    assert read_workspace_spend().total.cost_usd == 0.0


def test_the_admin_page_serves_the_figure():
    _store_run("congresswatch", "20260816T090000",
               [_stage_record("score", LlmUsage(cost_usd=12.34, calls=2), "2026-08-16T09:00:00")])

    r = client.get("/admin/spend")

    assert r.status_code == 200
    assert "$12.34" in r.text


def test_unknown_codex_usage_is_shown_as_unknown_not_zero():
    _store_run("congresswatch", "20260816T090000", [
        _stage_record(
            "score",
            LlmUsage(
                input_tokens=None, output_tokens=None, cost_usd=None,
                calls=1, model="gpt-5.6-terra",
            ),
            "2026-08-16T09:00:00",
        ),
    ])

    spend = read_workspace_spend()
    page = client.get("/admin/spend")

    assert spend.total.cost_usd is None
    assert spend.total.input_tokens is None
    assert page.status_code == 200
    assert "unknown" in page.text
