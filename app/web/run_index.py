"""The runs index rows: when a run happened, the version it pinned, how long it
took, and how it came out (the same stage strip the run page draws, under the
run's outcome in words). The run id is no longer a column — it is the row's
link target."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ValidationError

from app.core.errors import RunManifestNotJson
from app.core.run_status import RunStatus
from app.models.run_manifest import find_manifest_backed_run_dirs, read_run_manifest
from app.web.loading import runs_dir
from app.web.run_header import VersionNote, describe_run_duration, read_version_note
from app.web.stage_strip import StageStrip, build_stage_strip, describe_stage_tallies

_UNREADABLE_STATUS = "corrupt"


class RunIndexRow(BaseModel):
    """`strip` is None for a run whose manifest could not be parsed."""

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


def build_run_index_rows(project: str) -> list[RunIndexRow]:
    """One row per manifest-backed run of `project`, newest first."""
    seen_versions: dict[str, VersionNote] = {}
    return [
        _build_row(project, run, seen_versions)
        for run in reversed(find_manifest_backed_run_dirs(runs_dir(project)))
    ]


def describe_run_outcome(status: str) -> str:
    """A run's status in the reader's words, or the raw status this reader has no word for."""
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
    project: str, run: Path, seen_versions: dict[str, VersionNote]
) -> RunIndexRow:
    try:
        manifest = read_run_manifest(run)
    except (RunManifestNotJson, ValidationError):
        # An identity-only row rather than counts it never read, so one unreadable
        # run never takes the index down with it. No test-run filter here on
        # purpose: the index LISTS test runs (flagged), the dashboard count omits them.
        return RunIndexRow(run_id=run.name, status=_UNREADABLE_STATUS)
    persisted = manifest.to_dict()
    strip = build_stage_strip(persisted)
    return RunIndexRow(
        run_id=run.name,
        status=str(manifest.status),
        started_at=manifest.started_at,
        duration=describe_run_duration(persisted),
        version=_read_version(project, manifest.workflow_version, seen_versions),
        strip=strip,
        result_summary=describe_stage_tallies(strip),
        outcome=describe_run_outcome(str(manifest.status)),
        is_test_run=manifest.parameters.is_test_run,
    )


def _read_version(
    project: str, version_id: str | None, seen: dict[str, VersionNote]
) -> VersionNote:
    """Resolved once per distinct version id, not once per run listed."""
    key = version_id or ""
    if key not in seen:
        seen[key] = read_version_note(project, version_id)
    return seen[key]
