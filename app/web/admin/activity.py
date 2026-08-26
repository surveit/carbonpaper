"""Counted off records this app already writes; it adds no tracking and stores nothing."""
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
from app.services.project import list_visible_projects
from app.services.run import RunEntry, list_every_run_entry
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

    steps: list[FunnelStep]


class DayCount(BaseModel):
    day: str
    count: int


class DayChart(BaseModel):
    label: str
    # What one count is, and which stamp dates it.
    note: str
    days: list[DayCount]
    total: int
    peak: int


class LabelledCount(BaseModel):
    label: str
    count: int


class RunTotals(BaseModel):
    """What qualifies the run figures: the runs held apart, and the ones never read."""

    halted: int
    pinning_no_version: int
    # A project id no VISIBLE record covers: private, deleted, or never recorded.
    outside_any_visible_project: int
    # Manifests this app can no longer parse: unread, never counted as zero.
    unreadable: int
    eval_area: int


class ActivityReading(BaseModel):
    projects: Funnel
    runs: Funnel
    # Four counts on ONE shared day axis, so the four read against each other.
    charts: list[DayChart]
    run_status: list[LabelledCount]
    run_totals: RunTotals
    first_day: str | None
    last_day: str | None
    versioned_projects: int
    uploaded_files: int
    uploaded_bytes: int


def read_instance_activity() -> ActivityReading:
    entries = list_every_run_entry()
    production = [entry for entry in entries if entry.area == PRODUCTION_RUNS]
    runs = [entry.manifest for entry in production if entry.manifest is not None]
    projects = list_visible_projects()
    files = ProjectFile.list()
    visible = {record.id for record in projects}
    axis = _build_day_axis(projects, runs, files)
    return ActivityReading(
        projects=_build_project_funnel(visible, runs),
        runs=_build_run_funnel(runs),
        charts=_build_charts(projects, runs, files, axis),
        run_status=_count_run_statuses(runs),
        run_totals=_count_run_totals(entries, production, runs, visible),
        first_day=axis[0] if axis else None,
        last_day=axis[-1] if axis else None,
        versioned_projects=len(visible & _read_versioned_projects()),
        uploaded_files=len(files),
        uploaded_bytes=sum(record.byte_count for record in files),
    )


def _build_project_funnel(visible: set[str], runs: list[RunManifest]) -> Funnel:
    # A working copy is written the first time stages are saved; create_project writes none.
    authored = visible & (set(WorkingCopy.list_ids()) | _read_versioned_projects())
    ran = authored & {run.project for run in runs}
    published = ran & {run.project for run in runs if _published_a_file(run)}
    return _build_funnel([
        ("Projects a reader may open",
         "a project record that is neither private nor deleted", len(visible)),
        ("…that saved a workflow stage",
         "a working copy, or a stored workflow version", len(authored)),
        ("…that started a run", "a run manifest naming that project", len(ran)),
        ("…whose run completed a publish stage",
         "a publish stage recorded ok in that manifest", len(published)),
    ])


def _build_run_funnel(runs: list[RunManifest]) -> Funnel:
    terminal = [run for run in runs if run.status != RunStatus.RUNNING]
    published = [run for run in terminal if _published_a_file(run)]
    return _build_funnel([
        ("Runs started", "a readable run manifest outside the eval area", len(runs)),
        ("…that reached a terminal status",
         "a status other than running; a crash writes none", len(terminal)),
        ("…that completed a publish stage",
         "a publish stage recorded ok", len(published)),
    ])


def _build_funnel(rungs: list[tuple[str, str, int]]) -> Funnel:
    first = rungs[0][2] if rungs else 0
    return Funnel(steps=[
        FunnelStep(label=label, basis=basis, count=count,
                   share=count / first if first else 0.0)
        for label, basis, count in rungs
    ])


def _count_run_totals(
    entries: list[RunEntry], production: list[RunEntry],
    runs: list[RunManifest], visible: set[str],
) -> RunTotals:
    return RunTotals(
        halted=sum(1 for run in runs if run.halted_at),
        pinning_no_version=sum(1 for run in runs if not run.workflow_version),
        outside_any_visible_project=sum(1 for run in runs if run.project not in visible),
        unreadable=sum(1 for entry in production if entry.manifest is None),
        eval_area=len(entries) - len(production),
    )


def _build_charts(
    projects: list[Project], runs: list[RunManifest],
    files: list[ProjectFile], axis: list[str],
) -> list[DayChart]:
    return [
        _build_one_chart(label, note, stamps, axis)
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


def _build_one_chart(label: str, note: str, stamps: list[str], axis: list[str]) -> DayChart:
    by_day = Counter(stamp[:10] for stamp in stamps if stamp)
    return DayChart(
        label=label, note=note,
        # A Counter reads an absent day as 0 occurrences, which is the count, not a default.
        days=[DayCount(day=day, count=by_day[day]) for day in axis],
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
