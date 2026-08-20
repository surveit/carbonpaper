"""What one home-page project card says: its headline state and its run counts."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from app.core.run_status import RunStatus
from app.runtime.manifest import list_run_entries
from app.models.run_manifest import (
    records_a_test_run,
)


class ProjectStatus(enum.StrEnum):
    IN_PROGRESS = "in_progress"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    ERRORED = "errored"


PROJECT_STATUS_LABELS = {
    ProjectStatus.IN_PROGRESS: "In progress",
    ProjectStatus.AWAITING_REVIEW: "Awaiting review",
    ProjectStatus.COMPLETED: "Completed",
    ProjectStatus.ERRORED: "Errored",
}

# Every RunStatus, mapped onto the four words the card speaks in.
#
# WARNINGS completed: a validation warning is a finding the run produced, not a
# failure to produce one. CANCELLED is in progress: the run stopped short with
# nothing wrong, and the run page's own next action for it is Resume
# (`app.web.run_header.choose_run_cta`), so it is unfinished work, not a fault.
_RUN_STATUS_HEADLINES = {
    RunStatus.RUNNING: ProjectStatus.IN_PROGRESS,
    RunStatus.CANCELLED: ProjectStatus.IN_PROGRESS,
    RunStatus.AWAITING_REVIEW: ProjectStatus.AWAITING_REVIEW,
    RunStatus.OK: ProjectStatus.COMPLETED,
    RunStatus.WARNINGS: ProjectStatus.COMPLETED,
    RunStatus.ERRORS: ProjectStatus.ERRORED,
}


@dataclass(frozen=True)
class ProjectCard:
    # `id` addresses the project (it is its directory); `label` is only ever shown.
    id: str
    label: str
    has_document: bool
    has_workflow: bool
    has_schemas: bool
    # A stored version exists, so a run can pin one. This decides where the
    # card LINKS; `status` is what it SAYS, and the two move independently.
    is_ready: bool
    n_stages: int
    n_schemas: int
    n_runs: int
    n_test_runs: int
    status: ProjectStatus

    @property
    def status_label(self) -> str:
        return PROJECT_STATUS_LABELS[self.status]


@dataclass(frozen=True)
class RunCount:
    real: int
    tests: int
    # IN_PROGRESS for a project that has yet to produce a real run: it is still
    # being set up, which is the same unfinished state a running one is in.
    headline: ProjectStatus


def count_runs(project_id: str) -> RunCount:
    real = tests = 0
    headline: ProjectStatus | None = None
    for entry in reversed(list_run_entries(project_id)):
        # Read off the RAW payload, so a run written before a field was renamed
        # still counts; one that is not even JSON is dropped, not counted
        # 'corrupt' (as the project's own runs summary does), because a card's
        # count must not advertise a run nothing can be read off at all.
        manifest = entry.raw
        if manifest is None:
            continue
        if records_a_test_run(manifest):
            tests += 1
            continue
        real += 1
        if headline is None:
            headline = _read_headline(manifest)
    return RunCount(real=real, tests=tests,
                    headline=headline or ProjectStatus.IN_PROGRESS)


def _read_headline(manifest: dict[str, Any]) -> ProjectStatus | None:
    raw = manifest.get("status")
    # A run whose status this app cannot name still counts — it happened — but must
    # not be the headline; the next newest run that does name one supplies it.
    if not isinstance(raw, str):
        return None
    try:
        return _RUN_STATUS_HEADLINES[RunStatus(raw)]
    except ValueError:
        return None
