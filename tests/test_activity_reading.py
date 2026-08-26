"""The activity page counts nested rungs and states what no record answers."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.files import ProjectFile
from app.core.persistence import get_store
from app.core.run_status import RunStatus, StageStatus
from app.main import app
from app.models.records.project import Project
from app.models.records.run_manifest import RunManifest
from app.models.records.working_copy import WorkingCopy
from app.models.run_manifest import StageRecord
from app.models.stages.stage_base import StageType
from app.services.workspace import resolve_project_dir
from app.web.admin.activity import read_instance_activity

client = TestClient(app)


def _stage(stage_id: str, stage_type: StageType, status: StageStatus) -> StageRecord:
    return StageRecord(
        stage_id=stage_id, type=stage_type, status=status,
        input_validation_report=[], output_validation_report=None, output_row_count=1,
    )


def _store_run(
    project: str, run_id: str, *, status: RunStatus = RunStatus.OK,
    records: list[StageRecord] | None = None, started_at: str = "2026-08-16T09:00:00",
    area: str = "runs",
) -> None:
    RunManifest(
        id=RunManifest.compose_id(project, run_id, area),
        run_id=run_id, started_at=started_at, project=project, workflow_version="v1",
        human_review_queue_stats={}, status=status,
        stage_records=records if records is not None else [],
    ).save()


def _store_project(
    project: str, *, stages_saved: bool, created_at: str,
    private: bool = False, on_disk: bool = True,
) -> None:
    # A project IS its directory: the listing this page reads drops one without it.
    if on_disk:
        resolve_project_dir(project).mkdir(parents=True, exist_ok=True)
    Project(id=project, name=project, created_at=created_at, private=private).save()
    if stages_saved:
        WorkingCopy(id=project, stages=[]).save()


def test_each_project_rung_is_a_subset_of_the_one_above():
    _store_project("arrived", stages_saved=False, created_at="2026-08-16T09:00:00")
    _store_project("authored", stages_saved=True, created_at="2026-08-16T09:00:00")
    _store_project("ran", stages_saved=True, created_at="2026-08-16T09:00:00")
    _store_run("ran", "20260816T090000")

    steps = read_instance_activity().projects.steps

    assert [step.count for step in steps] == [3, 2, 1, 0]
    assert steps[0].share == 1.0


def test_a_run_with_no_terminal_status_is_started_but_not_finished():
    _store_project("live", stages_saved=True, created_at="2026-08-16T09:00:00")
    _store_run("live", "20260816T090000", status=RunStatus.RUNNING)
    _store_run("live", "20260816T100000", status=RunStatus.ERRORS)

    steps = read_instance_activity().runs.steps

    assert [step.count for step in steps] == [2, 1, 0]


def test_a_publish_stage_that_did_not_complete_is_not_a_published_run():
    _store_project("tried", stages_saved=True, created_at="2026-08-16T09:00:00")
    _store_run("tried", "20260816T090000", status=RunStatus.ERRORS,
               records=[_stage("write", StageType.publish, StageStatus.ERROR)])

    assert read_instance_activity().runs.steps[-1].count == 0


def test_a_run_that_published_then_failed_later_still_counts_as_published():
    _store_project("mixed", stages_saved=True, created_at="2026-08-16T09:00:00")
    _store_run("mixed", "20260816T090000", status=RunStatus.ERRORS, records=[
        _stage("write", StageType.publish, StageStatus.OK),
        _stage("score", StageType.llm_transform, StageStatus.ERROR),
    ])

    activity = read_instance_activity()

    assert activity.runs.steps[-1].count == 1
    assert activity.projects.steps[-1].count == 1


def test_a_run_naming_no_project_record_is_counted_and_sits_in_no_project_rung():
    _store_run("vanished", "20260816T090000")

    activity = read_instance_activity()

    assert activity.run_totals.outside_any_visible_project == 1
    assert activity.runs.steps[0].count == 1
    assert [step.count for step in activity.projects.steps] == [0, 0, 0, 0]


def test_an_unparseable_manifest_is_counted_rather_than_passed_over():
    get_store().write("run", "broken/runs/20260816T090000", {"run_id": "no fields"})

    activity = read_instance_activity()

    assert activity.run_totals.unreadable == 1
    assert activity.runs.steps[0].count == 0


def test_the_day_axis_holds_every_calendar_day_including_one_with_nothing_on_it():
    _store_project("spanning", stages_saved=True, created_at="2026-08-16T09:00:00")
    _store_run("spanning", "20260818T090000", started_at="2026-08-18T09:00:00")

    activity = read_instance_activity()

    assert activity.first_day == "2026-08-16"
    assert activity.last_day == "2026-08-18"
    runs_started = next(c for c in activity.charts if c.label == "Runs started")
    assert [(day.day, day.count) for day in runs_started.days] == [
        ("2026-08-16", 0), ("2026-08-17", 0), ("2026-08-18", 1),
    ]


def test_eval_runs_are_counted_apart_from_every_run_figure():
    _store_project("scored", stages_saved=True, created_at="2026-08-16T09:00:00")
    _store_run("scored", "20260816T090000", area="eval_run")

    activity = read_instance_activity()

    assert activity.run_totals.eval_area == 1
    assert activity.runs.steps[0].count == 0
    assert activity.projects.steps[2].count == 0


def test_an_uploaded_file_is_counted_by_its_record_not_by_walking_the_disk():
    ProjectFile(sha256="0" * 64, filename="filings.csv", byte_count=2048,
                project_id="somewhere").save()

    activity = read_instance_activity()

    assert (activity.uploaded_files, activity.uploaded_bytes) == (1, 2048)


def test_the_page_states_what_it_cannot_answer_and_names_no_project():
    _store_project("beneficial_ownership_leak", stages_saved=True,
                   created_at="2026-08-16T09:00:00")
    _store_run("beneficial_ownership_leak", "20260816T090000")

    body = client.get("/admin/activity").text

    assert "beneficial_ownership_leak" not in body
    assert "What these records cannot say" in body
    assert "Nothing records a visit or a request" in body


def test_an_instance_holding_nothing_counts_nothing_rather_than_dividing_by_it():
    activity = read_instance_activity()

    assert activity.first_day is None
    assert [step.share for step in activity.projects.steps] == [0.0, 0.0, 0.0, 0.0]
    assert "Nothing recorded yet" in client.get("/admin/activity").text


def test_a_private_project_is_counted_nowhere_and_its_run_sits_outside():
    _store_project("hidden", stages_saved=True, created_at="2026-08-16T09:00:00",
                   private=True)
    _store_run("hidden", "20260816T090000")

    activity = read_instance_activity()

    assert [step.count for step in activity.projects.steps] == [0, 0, 0, 0]
    assert activity.runs.steps[0].count == 1
    assert activity.run_totals.outside_any_visible_project == 1


def test_a_deleted_project_drops_out_while_its_run_is_still_counted():
    _store_project("abandoned", stages_saved=True, created_at="2026-08-16T09:00:00",
                   on_disk=False)
    _store_run("abandoned", "20260816T090000")

    activity = read_instance_activity()

    assert activity.projects.steps[0].count == 0
    assert activity.runs.steps[0].count == 1
    assert activity.run_totals.outside_any_visible_project == 1


def test_the_page_says_the_project_count_is_a_floor_rather_than_a_total():
    body = client.get("/admin/activity").text

    assert "The project count is a floor, not a total" in body
