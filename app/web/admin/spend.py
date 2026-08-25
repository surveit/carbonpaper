"""What the workspace has spent on models, read off the two records that already
hold it: a run's per-stage `llm_usage` in its manifest, and a chat session's
per-turn `turn_spend`. Nothing here writes; the two writers are the executor and
`app.core.agent.turns`. What neither of them recorded is COUNTED and stated, never
filled in with a zero — see `SpendReading.unreadable_runs` / `.silent_sessions`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from enum import Enum

from pydantic import BaseModel

from app.core.agent.store import AgentSession
from app.core.agent.usage import LlmUsage
from app.models.records.run_manifest import PRODUCTION_RUNS
from app.services.project import list_project_listings
from app.services.run import RunEntry, list_every_run_entry
from app.web.admin.day_axis import days_spanned

# What a model's name is shown as where the record does not carry one. Every run
# before the `model` field existed is in this bucket, so it holds real money.
UNRECORDED_MODEL = "not recorded"

# The project cell for a session whose context names none — a chat opened from the
# home page belongs to no project.
NO_PROJECT = "no project"

# Project id -> the name to show it by. A project no listing shows says only its id.
ProjectNames = dict[str, str]


class SpendSource(str, Enum):
    run = "run"
    agent_session = "agent session"


class SpendEntry(BaseModel):
    # The stamp the spend is dated by: the stage's start for a run, the turn's end
    # for a session.
    at: str
    source: SpendSource
    project: str
    # What was paid for, in the words of the page it came from.
    label: str
    # Where to go look at it. None where the record is not reachable by URL — an
    # eval run needs its eval's id, which the run key does not carry.
    link: str | None
    usage: LlmUsage

    @property
    def day(self) -> str:
        return self.at[:10]

    @property
    def model(self) -> str:
        return self.usage.model or UNRECORDED_MODEL


class SpendCount(BaseModel):
    label: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    calls: int
    entries: int


class SpendReading(BaseModel):
    total: SpendCount
    by_day: list[SpendCount]
    by_source: list[SpendCount]
    by_model: list[SpendCount]
    by_project: list[SpendCount]
    # The largest single entries, newest first among equals — the run stages and
    # chat turns worth looking at.
    biggest: list[SpendEntry]
    # Runs whose manifest this app can no longer parse. Their spend is not in any
    # figure above, so the page says how many rather than implying there are none.
    unreadable_runs: int
    # Sessions holding no per-turn spend at all: every session that ran before
    # `turn_spend` existed, plus any whose turns reported no usage.
    silent_sessions: int


def read_workspace_spend(*, biggest: int = 25) -> SpendReading:
    runs = list_every_run_entry()
    sessions = AgentSession.list()
    names = {listing.id: listing.name for listing in list_project_listings()}
    entries = [*read_run_spend(runs, names), *read_session_spend(sessions, names)]
    return SpendReading(
        total=count_spend("everything", entries),
        by_day=_daily_counts(entries),
        by_source=_ranked(entries, lambda e: e.source.value),
        by_model=_ranked(entries, lambda e: e.model),
        by_project=_ranked(entries, lambda e: e.project),
        biggest=sorted(entries, key=lambda e: (-e.usage.cost_usd, e.at))[:biggest],
        unreadable_runs=sum(1 for run in runs if run.manifest is None),
        silent_sessions=sum(1 for session in sessions if not session.turn_spend),
    )


def read_run_spend(runs: list[RunEntry], names: ProjectNames) -> list[SpendEntry]:
    return [
        entry
        for run in runs
        for entry in _entries_in_run(run, names)
    ]


def read_session_spend(sessions: list[AgentSession], names: ProjectNames) -> list[SpendEntry]:
    return [
        SpendEntry(
            at=turn.created_at,
            source=SpendSource.agent_session,
            project=_project_label(session.context.get("project_id"), names),
            label=session.title,
            link=f"/chat/{session.id}",
            usage=turn.usage,
        )
        for session in sessions
        for turn in session.turn_spend
    ]


def count_spend(label: str, entries: list[SpendEntry]) -> SpendCount:
    return SpendCount(
        label=label,
        cost_usd=sum(e.usage.cost_usd for e in entries),
        input_tokens=sum(e.usage.input_tokens for e in entries),
        output_tokens=sum(e.usage.output_tokens for e in entries),
        calls=sum(e.usage.calls for e in entries),
        entries=len(entries),
    )


def _daily_counts(entries: list[SpendEntry]) -> list[SpendCount]:
    """Every calendar day in the span, so a quiet week reads as a gap rather than as adjacency."""
    by_day = dict(_grouped(entries, lambda e: e.day))
    if not by_day:
        return []
    return [count_spend(day, by_day.get(day, []))
            for day in days_spanned(min(by_day), max(by_day))]


def _project_label(project_id: object, names: ProjectNames) -> str:
    """Carries the id even when a name is known: two projects may share one name."""
    if not project_id:
        return NO_PROJECT
    name = names.get(str(project_id))
    return str(project_id) if name in (None, project_id) else f"{name} ({project_id})"


def _entries_in_run(run: RunEntry, names: ProjectNames) -> list[SpendEntry]:
    manifest = run.manifest
    if manifest is None:
        # Counted as unreadable by the caller; a manifest this app cannot parse
        # states no stage usage to read.
        return []
    return [
        SpendEntry(
            at=record.started_at or manifest.started_at,
            source=SpendSource.run,
            project=_project_label(run.project, names),
            label=f"{run.run_id} · {record.stage_id}",
            link=f"/runs/{run.run_id}" if run.area == PRODUCTION_RUNS else None,
            usage=record.llm_usage,
        )
        for record in manifest.stage_records
        if record.llm_usage is not None
    ]


def _ranked(entries: list[SpendEntry], key: Callable[[SpendEntry], str]) -> list[SpendCount]:
    """Dearest first: what a spend page is read for is where the money went."""
    counts = [count_spend(label, group) for label, group in _grouped(entries, key)]
    return sorted(counts, key=lambda c: -c.cost_usd)


def _grouped(
    entries: list[SpendEntry], key: Callable[[SpendEntry], str]
) -> list[tuple[str, list[SpendEntry]]]:
    """Ascending by label, which is what puts `by_day` in date order."""
    groups: dict[str, list[SpendEntry]] = defaultdict(list)
    for entry in entries:
        groups[key(entry)].append(entry)
    return sorted(groups.items())
