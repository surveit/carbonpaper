"""The project overview: the run you could hand someone, and what stands between."""
from __future__ import annotations

from urllib.parse import urlencode
from typing import Literal

from pydantic import BaseModel

from app.web.figure_text import render_figure
from app.core.run_status import RunStatus
from app.models.records.workflow_version import WorkflowVersion
from app.services import methodology, run as run_service, versioning
from app.services.errors import WorkflowLoadError
from app.services.workspace import resolve_run_dir
from app.web.run_index import RunIndexRow, RunInputCell, StageRowCap, build_run_index_rows
from app.web.run_published import RunPublished, read_published_outputs

DeliverableState = Literal["clean", "warned", "no_runs"]

_RUN_NEWEST = "Run the newest version of this workflow."
_RUN_UNCAPPED = "Re-run this workflow with no row caps."
_DECLARE_FIGURES = "Declare this workflow's summary figures as workflow outputs, then run it."
_WHY_ERRORED = "Why did the last run error?"
_WRITE_METHODOLOGY = "Write a methodology document for this project from its workflow."


class VersionsRead(BaseModel):
    """A stored version written before a model changed cannot be parsed, and must not 500 a page."""

    versions: list[WorkflowVersion] = []
    problem: str | None = None


def read_versions(project_id: str) -> VersionsRead:
    try:
        return VersionsRead(versions=versioning.list_versions(project_id))
    except WorkflowLoadError as exc:
        return VersionsRead(problem=str(exc).splitlines()[0])


class OverviewCheck(BaseModel):
    ok: bool
    headline: str
    detail: str
    action: OverviewCheckAction | None = None


class OverviewCheckAction(BaseModel):
    label: str
    href: str
    kind: Literal["chat", "go"]


class Deliverable(BaseModel):
    state: DeliverableState
    heading: str
    run_id: str | None = None
    run_href: str = ""
    started_at: str = ""
    duration: str = ""
    stage_line: str = ""
    status: str = ""
    published: RunPublished | None = None
    checks: list[OverviewCheck] = []
    version_message: str | None = None
    inputs: list[RunInputCell] = []
    stage_caps: list[StageRowCap] = []
    lead: str | None = None


class QueueRow(BaseModel):
    count: str
    tone: Literal["good", "warn", "bad", "info", "idle"]
    what: str
    why: str
    label: str
    href: str
    kind: Literal["chat", "go"]


class ProjectOverview(BaseModel):
    deliverable: Deliverable
    queue: list[QueueRow]


def build_project_overview(project_id: str) -> ProjectOverview:
    rows = [row for row in build_run_index_rows(project_id) if not row.is_test_run]
    versions = read_versions(project_id)
    return ProjectOverview(
        deliverable=build_deliverable(project_id, rows, versions),
        queue=build_queue(project_id, rows, versions),
    )


# ─── The deliverable slot ─────────────────────────────────────────────────────


def build_deliverable(
    project_id: str, rows: list[RunIndexRow], versions: VersionsRead
) -> Deliverable:
    if not rows:
        return Deliverable(
            state="no_runs", heading="No run yet", lead=describe_missing_runs(versions)
        )
    row = rows[0]
    manifest = run_service.read_run_status(project_id, row.run_id)
    published = read_published_outputs(
        project_id, row.run_id, resolve_run_dir(project_id, row.run_id), manifest
    )
    checks = build_checks(project_id, row, published)
    return Deliverable(
        state="warned" if any(not check.ok for check in checks) else "clean",
        heading="Latest run",
        run_id=row.run_id,
        run_href=f"/project/{project_id}/runs/{row.run_id}",
        started_at=row.started_at or "",
        duration=row.duration or "",
        stage_line=row.result_summary or "",
        status=row.status,
        published=published,
        checks=checks,
        version_message=row.version.message if row.version else None,
        inputs=row.inputs,
        stage_caps=row.stage_caps,
    )


# ─── What a reader should know before treating this run's figures as the answer ──


def build_checks(
    project_id: str, row: RunIndexRow, published: RunPublished
) -> list[OverviewCheck]:
    found = [
        find_windowed_warning(project_id, row),
        find_incomplete_warning(project_id, row),
        find_no_figures_warning(project_id, published),
    ]
    warnings = [warning for warning in found if warning is not None]
    return warnings or [
        OverviewCheck(
            ok=True, headline="Finished clean, over the whole input.",
            detail="Every stage validated its output against the schema its version declares, "
                   "and every input step read its whole file.",
        )
    ]


def find_windowed_warning(project_id: str, row: RunIndexRow) -> OverviewCheck | None:
    """A test run and a capped run fail the same way: complete over a slice of the rows."""
    if row.is_test_run:
        return OverviewCheck(
            ok=False, headline="This was a test run.",
            detail="A test run reads a window of the rows. It can finish every stage and write "
                   "the same files a production run writes, and its numbers are still not this "
                   "project's numbers.",
            action=ask_the_agent(project_id, "Run it whole", _RUN_UNCAPPED),
        )
    capped = [(cell.stage_id, cell.row_cap) for cell in row.inputs if cell.row_cap is not None]
    capped += [(cap.stage_id, cap.cap) for cap in row.stage_caps]
    if not capped:
        return None
    named = ", ".join(f"{stage} (first {render_figure(cap)} rows)" for stage, cap in sorted(capped))
    return OverviewCheck(
        ok=False, headline="This run was capped.",
        detail=f"{named} read a window of its input, so every figure counted below it counts "
               f"a slice.",
        action=ask_the_agent(project_id, "Run it whole", _RUN_UNCAPPED),
    )


def find_incomplete_warning(project_id: str, row: RunIndexRow) -> OverviewCheck | None:
    if row.status == RunStatus.OK:
        return None
    if row.status == RunStatus.AWAITING_REVIEW:
        return OverviewCheck(
            ok=False, headline="This run is waiting on a review.",
            detail="The stages behind the queue have not run, so what it has produced so far is "
                   "only part of the answer.",
            action=OverviewCheckAction(
                label="Review it", kind="go",
                href=f"/project/{project_id}/runs/{row.run_id}",
            ),
        )
    return OverviewCheck(
        ok=False,
        headline=("This run has not completed." if row.status == RunStatus.RUNNING
                  else f"This run ended {row.status}."),
        detail="Only a run that finished every stage cleanly produces the whole answer.",
        action=ask_the_agent(
            project_id, "Explain it", f"Why did the run {row.run_id} end {row.status}?"
        ),
    )


def find_no_figures_warning(project_id: str, published: RunPublished) -> OverviewCheck | None:
    if published:
        return None
    return OverviewCheck(
        ok=False, headline="This run produced no figures.",
        detail="A figure is declared on a stage and written while the run executes, so a run "
               "that did not carry the declaration never wrote the cell.",
        action=ask_the_agent(project_id, "Declare the figures", _DECLARE_FIGURES),
    )


def ask_the_agent(project_id: str, label: str, task: str) -> OverviewCheckAction:
    return OverviewCheckAction(
        label=label, kind="chat", href=build_chat_href(project_id, task)
    )


def describe_missing_runs(versions: VersionsRead) -> str:
    if versions.problem is None and not versions.versions:
        return "No version has been stored, so nothing can be run yet."
    return "This workflow has never run in production."


# ─── The queue ────────────────────────────────────────────────────────────────


def build_queue(
    project_id: str, rows: list[RunIndexRow], versions: VersionsRead
) -> list[QueueRow]:
    found = [
        find_reviews_waiting(project_id, rows),
        find_runs_running(project_id, rows),
        find_newest_version_never_run(project_id, rows, versions),
        find_runs_that_errored(project_id, rows),
        find_missing_methodology(project_id),
        find_unreadable_version(project_id, versions),
    ]
    return [row for row in found if row is not None]


def find_reviews_waiting(project_id: str, rows: list[RunIndexRow]) -> QueueRow | None:
    waiting = [row for row in rows if row.status == RunStatus.AWAITING_REVIEW]
    if not waiting:
        return None
    return QueueRow(
        count=str(len(waiting)), tone="info",
        what=f"run{'s' if len(waiting) != 1 else ''} halted for review",
        why="a person has to decide the queued rows before the stages behind them run",
        label="Review", href=f"/project/{project_id}/runs?status=awaiting_review", kind="go",
    )


def find_unreadable_version(project_id: str, versions: VersionsRead) -> QueueRow | None:
    """Nothing a reader can hand-edit; the agent can read the payload and re-save it."""
    if versions.problem is None:
        return None
    return QueueRow(
        count="!", tone="bad", what="A stored version cannot be read",
        why=versions.problem,
        label="Repair it", kind="chat",
        href=build_chat_href(project_id, f"A stored version will not parse: {versions.problem}"),
    )


def find_newest_version_never_run(
    project_id: str, rows: list[RunIndexRow], versions: VersionsRead
) -> QueueRow | None:
    """Only the newest one matters — nobody wants to run a version that has been superseded."""
    if not versions.versions:
        return None
    newest = max(versions.versions, key=lambda version: version.version_id)
    if any(row.version and row.version.version_id == newest.version_id for row in rows):
        return None
    return QueueRow(
        count="", tone="warn", what="The newest version has never run",
        why=(newest.message or newest.version_id),
        label="Run it", kind="chat",
        href=build_chat_href(project_id, _RUN_NEWEST),
    )


def find_runs_running(project_id: str, rows: list[RunIndexRow]) -> QueueRow | None:
    """Elapsed is the only tell: nothing records a heartbeat, so a crashed run reads as running."""
    running = [row for row in rows if row.status == RunStatus.RUNNING]
    if not running:
        return None
    longest = max((row.duration or "" for row in running), key=len)
    return QueueRow(
        count=str(len(running)), tone="info",
        what=f"run{'s' if len(running) != 1 else ''} running",
        why=f"the longest for {longest}" if longest else "elapsed unrecorded",
        label="Watch them", href=f"/project/{project_id}/runs?status=running", kind="go",
    )


def find_runs_that_errored(project_id: str, rows: list[RunIndexRow]) -> QueueRow | None:
    errored = [row for row in rows if row.status == RunStatus.ERRORS]
    if not errored:
        return None
    return QueueRow(
        count=str(len(errored)), tone="idle",
        what=f"run{'s' if len(errored) != 1 else ''} errored",
        why=f"the last on {(errored[0].started_at or 'an unrecorded date')[:10]}",
        label="Explain the errors", kind="chat",
        href=build_chat_href(project_id, _WHY_ERRORED),
    )


def find_missing_methodology(project_id: str) -> QueueRow | None:
    if methodology.exists(project_id):
        return None
    return QueueRow(
        count="", tone="warn", what="No methodology document",
        why="a review packet would open on a blank page, and nothing states what this "
            "project establishes",
        label="Write the document", kind="chat",
        href=build_chat_href(project_id, _WRITE_METHODOLOGY),
    )


def build_chat_href(project_id: str, task: str) -> str:
    return "/chat/agent/editing/new?" + urlencode({"project_id": project_id, "task": task})
