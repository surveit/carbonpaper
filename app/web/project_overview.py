"""The project overview: the run you could hand someone, and what stands between."""
from __future__ import annotations

from urllib.parse import urlencode
from typing import Literal

from pydantic import BaseModel

from app.core.run_status import RunStatus
from app.models.records.workflow_version import WorkflowVersion
from app.services import methodology, run as run_service, versioning
from app.services.errors import WorkflowLoadError
from app.services.run_publication import (
    PublishRefusal,
    find_publish_refusals,
    read_published_run,
)
from app.services.workspace import resolve_run_dir
from app.web.run_index import RunIndexRow, build_run_index_rows
from app.web.run_published import RunPublished, read_published_outputs

DeliverableState = Literal["published", "publishable", "refused", "no_runs"]


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


class OverviewInput(BaseModel):
    filename: str
    size: str
    row_cap: int | None


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
    inputs: list[OverviewInput] = []
    packet_href: str | None = None
    publish_href: str | None = None
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
    purpose: str | None
    deliverable: Deliverable
    queue: list[QueueRow]


def build_project_overview(project_id: str) -> ProjectOverview:
    rows = [row for row in build_run_index_rows(project_id) if not row.is_test_run]
    versions = read_versions(project_id)
    return ProjectOverview(
        purpose=methodology.read_opening_paragraph(project_id),
        deliverable=build_deliverable(project_id, rows, versions),
        queue=build_queue(project_id, rows, versions),
    )


# ─── The deliverable slot ─────────────────────────────────────────────────────


def build_deliverable(
    project_id: str, rows: list[RunIndexRow], versions: VersionsRead
) -> Deliverable:
    row = choose_deliverable_run(project_id, rows)
    if row is None:
        return Deliverable(
            state="no_runs",
            heading="Nothing to hand over",
            lead=describe_missing_runs(versions),
        )
    refusals = find_publish_refusals(project_id, row.run_id)
    is_published = read_published_run(project_id, row.run_id) is not None
    manifest = run_service.read_run_status(project_id, row.run_id)
    return Deliverable(
        state="published" if is_published else "refused" if refusals else "publishable",
        heading=(
            "What you can hand someone" if is_published
            else "Ready to publish" if not refusals
            else "Nothing to publish"
        ),
        run_id=row.run_id,
        run_href=f"/project/{project_id}/runs/{row.run_id}",
        started_at=row.started_at or "",
        duration=row.duration or "",
        stage_line=row.result_summary or "",
        status=row.status,
        published=read_published_outputs(
            project_id, row.run_id, resolve_run_dir(project_id, row.run_id), manifest
        ),
        checks=build_checks(refusals),
        version_message=row.version.message if row.version else None,
        inputs=[
            OverviewInput(filename=cell.filename, size=cell.size, row_cap=cell.row_cap)
            for cell in row.inputs
        ],
        packet_href=(
            f"/project/{project_id}/runs/{row.run_id}/packet.zip" if is_published else None
        ),
        publish_href=(
            f"/project/{project_id}/runs/{row.run_id}/publish"
            if not refusals and not is_published else None
        ),
    )


def choose_deliverable_run(project_id: str, rows: list[RunIndexRow]) -> RunIndexRow | None:
    """The published run if there is one, so publishing pins what the page leads with."""
    for row in rows:
        if read_published_run(project_id, row.run_id) is not None:
            return row
    return rows[0] if rows else None


def build_checks(refusals: list[PublishRefusal]) -> list[OverviewCheck]:
    if refusals:
        return [
            OverviewCheck(ok=False, headline=refusal.headline, detail=refusal.detail)
            for refusal in refusals
        ]
    return [
        OverviewCheck(
            ok=True,
            headline="No row caps.",
            detail="Every input step read its whole file.",
        ),
        OverviewCheck(
            ok=True,
            headline="Finished clean.",
            detail="Every stage validated its output against the schema its version declares.",
        ),
    ]


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
        find_unreadable_version(versions),
        find_versions_never_run(project_id, rows, versions),
        find_runs_still_marked_running(project_id, rows),
        find_runs_that_errored(project_id, rows),
        find_missing_methodology(project_id),
        find_unpublished_versions(project_id, versions),
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
        label="Review", href=f"/project/{project_id}/runs", kind="go",
    )


def find_unreadable_version(versions: VersionsRead) -> QueueRow | None:
    if versions.problem is None:
        return None
    return QueueRow(
        count="!", tone="bad", what="A stored version cannot be read",
        why=versions.problem,
        label="Open the versions", href="#", kind="go",
    )


def find_versions_never_run(
    project_id: str, rows: list[RunIndexRow], versions: VersionsRead
) -> QueueRow | None:
    pinned = next(
        (row.version.version_id for row in rows
         if row.version and row.version.version_id), None
    )
    if pinned is None:
        return None
    newer = [v for v in versions.versions if v.version_id > pinned]
    if not newer:
        return None
    return QueueRow(
        count=str(len(newer)), tone="warn",
        what=f"version{'s' if len(newer) != 1 else ''} never run",
        why="the working copy has moved since anything executed",
        label="Ask the agent to run it",
        href=build_chat_href(project_id, "Run the newest version of this workflow."),
        kind="chat",
    )


def find_runs_still_marked_running(project_id: str, rows: list[RunIndexRow]) -> QueueRow | None:
    stuck = [row for row in rows if row.status == RunStatus.RUNNING]
    if not stuck:
        return None
    return QueueRow(
        count=str(len(stuck)), tone="warn",
        what=f"run{'s' if len(stuck) != 1 else ''} still marked running",
        why="no terminal status was ever written, so they hold no result and never will",
        label="Ask the agent to clear them",
        href=build_chat_href(project_id, "Some runs are stuck marked running. What happened?"),
        kind="chat",
    )


def find_runs_that_errored(project_id: str, rows: list[RunIndexRow]) -> QueueRow | None:
    errored = [row for row in rows if row.status == RunStatus.ERRORS]
    if not errored:
        return None
    return QueueRow(
        count=str(len(errored)), tone="idle",
        what=f"run{'s' if len(errored) != 1 else ''} errored",
        why=f"the last on {(errored[0].started_at or 'an unrecorded date')[:10]}",
        label="Ask what went wrong",
        href=build_chat_href(project_id, "Why did the last run error?"),
        kind="chat",
    )


def find_missing_methodology(project_id: str) -> QueueRow | None:
    if methodology.exists(project_id):
        return None
    return QueueRow(
        count="", tone="warn", what="No methodology document",
        why="a review packet would open on a blank page, and nothing states what this "
            "project establishes",
        label="Ask the agent to write one",
        href=build_chat_href(
            project_id, "Write a methodology document for this project from its workflow."
        ),
        kind="chat",
    )


def find_unpublished_versions(project_id: str, versions: VersionsRead) -> QueueRow | None:
    stored = versions.versions
    if not stored or any(version.published for version in stored):
        return None
    return QueueRow(
        count="", tone="warn", what="No version published",
        why="nothing is nominated as the snapshot an outsider should cite",
        label="Publish a version",
        href=f"/project/{project_id}/workflow/versions", kind="go",
    )


def build_chat_href(project_id: str, task: str) -> str:
    return "/chat/agent/editing/new?" + urlencode({"project_id": project_id, "task": task})
