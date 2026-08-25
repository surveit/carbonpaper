"""What this instance has been used for, counted off records it already writes: project
records, working copies, stored versions, uploaded files and run manifests. It adds no
tracking of its own — nothing here records a visit, a request or a person, so a question
needing one is named on the page rather than estimated. Read-only; it writes nothing.
"""
from __future__ import annotations

from collections import Counter

from pydantic import BaseModel

from app.core.files import ProjectFile
from app.core.run_status import RunStatus, StageStatus
from app.models import StageType
from app.models.records.project import Project
from app.models.records.run_manifest import PRODUCTION_RUNS, RunManifest
from app.models.records.workflow_version import WorkflowVersion
from app.models.records.working_copy import WorkingCopy
from app.services.run import list_every_run_entry
from app.web.admin.day_axis import days_spanned


class FunnelStep(BaseModel):
    label: str
    # What a record must carry to be counted here, so a step is checkable against the store.
    basis: str
    count: int
    # Of the FIRST step's count, the only denominator these records hold.
    share: float


class Funnel(BaseModel):
    """Every step is a subset of the one above it, so the drop between two is a real drop."""

    # What one counted thing is: a project, or a run.
    grain: str
    steps: list[FunnelStep]


class DayCount(BaseModel):
    day: str
    count: int


class Series(BaseModel):
    label: str
    # What one count is, and which stamp dates it.
    note: str
    days: list[DayCount]
    total: int
    peak: int


class LabelledCount(BaseModel):
    label: str
    count: int


class ActivityReading(BaseModel):
    projects: Funnel
    runs: Funnel
    # Four counts on ONE shared day axis, so the four read against each other.
    series: list[Series]
    run_status: list[LabelledCount]
    first_day: str | None
    last_day: str | None
    versioned_projects: int
    private_projects: int
    halted_runs: int
    runs_pinning_no_version: int
    eval_runs: int
    uploaded_files: int
    uploaded_bytes: int
    # Manifests this app can no longer parse: unread, never counted as zero.
    unreadable_runs: int
    # Runs under a project id no project record covers.
    runs_outside_a_project_record: int


def read_instance_activity() -> ActivityReading:
    entries = list_every_run_entry()
    production = [entry for entry in entries if entry.area == PRODUCTION_RUNS]
    runs = [entry.manifest for entry in production if entry.manifest is not None]
    projects = Project.list()
    files = ProjectFile.list()
    recorded = {record.id for record in projects}
    axis = _build_day_axis(projects, runs, files)
    return ActivityReading(
        projects=_build_project_funnel(recorded, runs),
        runs=_build_run_funnel(runs),
        series=_build_series(projects, runs, files, axis),
        run_status=_count_run_statuses(runs),
        first_day=axis[0] if axis else None,
        last_day=axis[-1] if axis else None,
        versioned_projects=len(recorded & _read_versioned_projects()),
        private_projects=sum(1 for record in projects if record.private),
        halted_runs=sum(1 for run in runs if run.halted_at),
        runs_pinning_no_version=sum(1 for run in runs if not run.workflow_version),
        eval_runs=sum(1 for entry in entries if entry.area != PRODUCTION_RUNS),
        uploaded_files=len(files),
        uploaded_bytes=sum(record.byte_count for record in files),
        unreadable_runs=sum(1 for entry in production if entry.manifest is None),
        runs_outside_a_project_record=sum(1 for run in runs if run.project not in recorded),
    )


def _build_project_funnel(recorded: set[str], runs: list[RunManifest]) -> Funnel:
    # A working copy is written the first time stages are saved, so its presence IS
    # the authoring act; create_project writes none.
    authored = recorded & (set(WorkingCopy.list_ids()) | _read_versioned_projects())
    ran = authored & {run.project for run in runs}
    published = ran & {run.project for run in runs if _published_a_file(run)}
    return _build_funnel("project", [
        ("Projects recorded", "a project record in the store", len(recorded)),
        ("…that saved a workflow stage",
         "a working copy, or a stored workflow version", len(authored)),
        ("…that started a run", "a run manifest naming that project", len(ran)),
        ("…whose run completed a publish stage",
         "a publish stage recorded ok in that manifest", len(published)),
    ])


def _build_run_funnel(runs: list[RunManifest]) -> Funnel:
    terminal = [run for run in runs if run.status != RunStatus.RUNNING]
    published = [run for run in terminal if _published_a_file(run)]
    return _build_funnel("run", [
        ("Runs started", "a readable run manifest outside the eval area", len(runs)),
        ("…that reached a terminal status",
         "a status other than running; a crash writes none", len(terminal)),
        ("…that completed a publish stage",
         "a publish stage recorded ok", len(published)),
    ])


def _build_funnel(grain: str, rungs: list[tuple[str, str, int]]) -> Funnel:
    first = rungs[0][2] if rungs else 0
    return Funnel(grain=grain, steps=[
        FunnelStep(label=label, basis=basis, count=count,
                   share=count / first if first else 0.0)
        for label, basis, count in rungs
    ])


def _build_series(
    projects: list[Project], runs: list[RunManifest],
    files: list[ProjectFile], axis: list[str],
) -> list[Series]:
    return [
        _build_one_series(label, note, stamps, axis)
        for label, note, stamps in _read_dated_counts(projects, runs, files)
    ]


def _read_dated_counts(
    projects: list[Project], runs: list[RunManifest], files: list[ProjectFile],
) -> list[tuple[str, str, list[str]]]:
    return [
        ("Projects recorded", "one project record, dated by when the RECORD was written",
         [record.created_at for record in projects]),
        ("Files uploaded", "one uploaded-file record, dated by when it was stored",
         [record.created_at for record in files]),
        ("Runs started", "one run manifest, dated by its own started_at",
         [run.started_at for run in runs]),
        ("Runs that completed a publish stage", "the same run, dated the same way",
         [run.started_at for run in runs if _published_a_file(run)]),
    ]


def _build_day_axis(
    projects: list[Project], runs: list[RunManifest], files: list[ProjectFile],
) -> list[str]:
    stamps = [*(r.created_at for r in projects), *(r.created_at for r in files),
              *(run.started_at for run in runs)]
    days = sorted({stamp[:10] for stamp in stamps if stamp})
    return days_spanned(days[0], days[-1]) if days else []


def _build_one_series(label: str, note: str, stamps: list[str], axis: list[str]) -> Series:
    by_day = Counter(stamp[:10] for stamp in stamps if stamp)
    return Series(
        label=label, note=note,
        days=[DayCount(day=day, count=by_day.get(day, 0)) for day in axis],
        total=sum(by_day.values()),
        peak=max(by_day.values(), default=0),
    )


def _count_run_statuses(runs: list[RunManifest]) -> list[LabelledCount]:
    counted = Counter(str(run.status) for run in runs)
    return [LabelledCount(label=label, count=count) for label, count in counted.most_common()]


def _read_versioned_projects() -> set[str]:
    """Ids only — a version document embeds its whole stage list and is not read here."""
    return {key.split("/", 1)[0] for key in WorkflowVersion.list_ids()}


def _published_a_file(run: RunManifest) -> bool:
    return any(record.type == StageType.publish and record.status == StageStatus.OK
               for record in run.stage_records)
