"""The runs index rows: when a run happened, the version it pinned, how long it
took, and how it came out (the same stage strip the run page draws, under the
run's outcome in words). The run id is no longer a column — it is the row's
link target."""

from __future__ import annotations


from pydantic import BaseModel

from app.core.run_status import RunStatus
from app.models.run_manifest import UNREADABLE_RUN_STATUS
from app.runtime.manifest import RunEntry, list_run_entries
from app.services.run_manifest_metadata import read_archived_run_ids
from app.web.run_header import VersionNote, describe_run_duration, read_version_note
from app.web.stage_strip import StageStrip, build_stage_strip, describe_stage_counts


class RunIndexRow(BaseModel):
    run_id: str
    status: str
    started_at: str | None = None
    duration: str | None = None
    version: VersionNote | None = None
    strip: StageStrip | None = None
    # The strip's counts in words, for the result cell's tooltip: the squares
    # carry the colour, this carries what the colours say.
    result_summary: str = ""
    # The run's own status in the reader's words, under the strip. Empty for a
    # manifest that could not be read — that cell states the unreadability instead.
    outcome: str = ""
    is_test_run: bool = False


# The runs index's three mutually exclusive buckets. Archived takes priority over
# test — archiving is an explicit action that pulls a run off every other list,
# so an archived test run shows only under RUN_VIEW_ARCHIVED.
RUN_VIEW_PRODUCTION = "production"
RUN_VIEW_TEST = "test"
RUN_VIEW_ARCHIVED = "archived"
RUN_VIEWS = (RUN_VIEW_PRODUCTION, RUN_VIEW_TEST, RUN_VIEW_ARCHIVED)


def build_run_index_rows(project_id: str, *, view: str | None = None) -> list[RunIndexRow]:
    """`view=None` lists every non-archived run; pass a RUN_VIEWS value for one bucket."""
    hidden = read_archived_run_ids(project_id)
    seen_versions: dict[str, VersionNote] = {}
    return [
        _build_row(project_id, entry, seen_versions)
        for entry in reversed(list_run_entries(project_id))
        if _matches_view(entry, hidden, view)
    ]


def count_archived_runs(project_id: str) -> int:
    return len(read_archived_run_ids(project_id))


def count_runs_by_view(project_id: str) -> dict[str, int]:
    hidden = read_archived_run_ids(project_id)
    counts = {view: 0 for view in RUN_VIEWS}
    for entry in list_run_entries(project_id):
        counts[_run_view(entry, hidden)] += 1
    return counts


def _matches_view(entry: RunEntry, hidden: set[str], view: str | None) -> bool:
    if view is None:
        return entry.run_id not in hidden
    return _run_view(entry, hidden) == view


def _run_view(entry: RunEntry, hidden: set[str]) -> str:
    if entry.run_id in hidden:
        return RUN_VIEW_ARCHIVED
    if entry.manifest is not None and entry.manifest.parameters.is_test_run:
        return RUN_VIEW_TEST
    return RUN_VIEW_PRODUCTION


def describe_run_outcome(status: str) -> str:
    return _OUTCOME_WORDS.get(status, status)


# Keyed by the stored string, which is what a manifest carries and what the row
# holds — an enum-keyed lookup would miss every one of them.
_OUTCOME_WORDS = {
    RunStatus.RUNNING.value: "In progress",
    RunStatus.OK.value: "Complete",
    RunStatus.WARNINGS.value: "Complete, with warnings",
    RunStatus.ERRORS.value: "Error",
    RunStatus.AWAITING_REVIEW.value: "Awaiting review",
    RunStatus.CANCELLED.value: "Cancelled",
}


def _build_row(
    project_id: str, entry: RunEntry, seen_versions: dict[str, VersionNote]
) -> RunIndexRow:
    if entry.manifest is None:
        # An identity-only row rather than counts it never read, so one unreadable
        # run never takes the index down with it. No test-run filter here on
        # purpose: the index LISTS test runs (flagged), the dashboard count omits them.
        return RunIndexRow(run_id=entry.run_id, status=UNREADABLE_RUN_STATUS)
    manifest = entry.manifest
    persisted = manifest.to_dict()
    strip = build_stage_strip(persisted)
    return RunIndexRow(
        run_id=entry.run_id,
        status=str(manifest.status),
        started_at=manifest.started_at,
        duration=describe_run_duration(persisted),
        version=_read_version(project_id, manifest.workflow_version, seen_versions),
        strip=strip,
        result_summary=describe_stage_counts(strip),
        outcome=describe_run_outcome(str(manifest.status)),
        is_test_run=manifest.parameters.is_test_run,
    )


def _read_version(
    project_id: str, version_id: str | None, seen: dict[str, VersionNote]
) -> VersionNote:
    key = version_id or ""
    if key not in seen:
        seen[key] = read_version_note(project_id, version_id)
    return seen[key]
