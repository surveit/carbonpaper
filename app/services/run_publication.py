"""Publishing a run: what refuses one, and the record that says it happened."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from app.core.run_status import RunStatus
from app.models.records.published_run import PublishedRun
from app.models.records.workflow_output import WorkflowOutput
from app.models.run_manifest import records_a_test_run
from app.services import run as run_service
from app.services.errors import RunNotPublishable


class PublishRefusal(BaseModel):
    kind: Literal["windowed", "incomplete", "no_figures"]
    headline: str
    detail: str


def find_publish_refusals(project_id: str, run_id: str) -> list[PublishRefusal]:
    manifest = dict(run_service.read_run_status(project_id, run_id))
    found = [
        find_window_refusal(manifest),
        find_incomplete_refusal(manifest),
        find_missing_figure_refusal(run_id),
    ]
    return [refusal for refusal in found if refusal is not None]


def publish_run(project_id: str, run_id: str) -> PublishedRun:
    refusals = find_publish_refusals(project_id, run_id)
    if refusals:
        raise RunNotPublishable(run_id, [r.headline for r in refusals])
    already = read_published_run(project_id, run_id)
    if already is not None:
        return already
    record = PublishedRun(project_id=project_id, run_id=run_id)
    record.save()
    return record


def withdraw_run(project_id: str, run_id: str) -> None:
    record = read_published_run(project_id, run_id)
    if record is not None:
        PublishedRun.delete(record.id)


def read_published_run(project_id: str, run_id: str) -> PublishedRun | None:
    found = PublishedRun.find(project_id=project_id, run_id=run_id)
    return found[0] if found else None


def find_newest_published_run(project_id: str) -> PublishedRun | None:
    # Run ids are strftime timestamps, so the lexical max is the newest.
    published = PublishedRun.find(project_id=project_id)
    return max(published, key=lambda record: record.run_id) if published else None


# ─── The three refusals ───────────────────────────────────────────────────────


def find_window_refusal(manifest: dict[str, Any]) -> PublishRefusal | None:
    """A test run and a capped run fail the same way: complete over a slice of the rows."""
    if records_a_test_run(manifest):
        return PublishRefusal(
            kind="windowed",
            headline="This was a test run.",
            detail="A test run reads a window of the rows. It can finish every stage and write "
                   "the same files a production run writes, and its numbers are still not this "
                   "project's numbers.",
        )
    capped = read_row_caps(manifest)
    if not capped:
        return None
    named = ", ".join(f"{stage} (first {cap:,} rows)" for stage, cap in capped)
    return PublishRefusal(
        kind="windowed",
        headline="This run was capped.",
        detail=f"{named} read a window of its input, so every figure counted below it counts "
               f"a slice.",
    )


def find_incomplete_refusal(manifest: dict[str, Any]) -> PublishRefusal | None:
    status = manifest.get("status")
    if status == RunStatus.OK:
        return None
    return PublishRefusal(
        kind="incomplete",
        headline=_describe_incompleteness(status),
        detail="Only a run that finished every stage cleanly can be the source of a published "
               "figure.",
    )


def _describe_incompleteness(status: object) -> str:
    """A running run has not ENDED anything, so the word every other status takes is wrong."""
    if status == RunStatus.RUNNING:
        return "This run has not completed."
    return f"This run ended {status or 'unknown'}."


def find_missing_figure_refusal(run_id: str) -> PublishRefusal | None:
    if read_run_output_count(run_id):
        return None
    return PublishRefusal(
        kind="no_figures",
        headline="This run produced no figures.",
        detail="A figure is declared on a stage and written while the run executes, so there is "
               "nothing to publish and nothing to mint after the fact. Declaring them and "
               "running again is the way through.",
    )


def read_row_caps(manifest: dict[str, Any]) -> list[tuple[str, int]]:
    parameters = manifest.get("parameters")
    limits = parameters.get("limits") if isinstance(parameters, dict) else None
    return sorted(limits.items()) if isinstance(limits, dict) else []


def read_run_output_count(run_id: str) -> int:
    """A run id sits inside the citation, which find() cannot select on."""
    return sum(1 for output in WorkflowOutput.list() if output.citation.run_id == run_id)
