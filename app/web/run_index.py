"""One row per run: what it was called, what it read, what it pinned, took and cost."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable

from pydantic import BaseModel

from app.core.run_status import RunStatus
from app.models.run_manifest import UNREADABLE_RUN_STATUS, InputBinding, read_input_bindings
from app.runtime.manifest import RunEntry, list_run_entries
from app.models.records.run_manifest import RunManifest
from app.services.run_manifest_metadata import read_archived_run_ids, read_run_names
from app.web.file_sizes import describe_bytes
from app.web.run_header import VersionNote, describe_run_duration, read_version_note
from app.web.stage_strip import StageStrip, build_stage_strip, describe_stage_counts


class RunInputCell(BaseModel):
    stage_id: str
    filename: str
    size: str
    sha256: str | None = None
    # One filename over two sets of bytes: the name alone is not an identity.
    hash_disambiguates: bool = False
    # How many of the file's rows this run read, when it did not read them all.
    row_cap: int | None = None


class StageRowCap(BaseModel):
    """A cap on a stage that bound no file — its rows come from upstream, not disk."""

    stage_id: str
    cap: int


class RunIndexRow(BaseModel):
    run_id: str
    status: str
    # Empty when never named; the row falls back to `started_at`.
    name: str = ""
    started_at: str | None = None
    duration: str | None = None
    # Summed over the stage records; 0.0 is a run that called no model.
    cost_usd: float = 0.0
    version: VersionNote | None = None
    strip: StageStrip | None = None
    # The strip's counts in words, for the result cell's tooltip: the squares
    # carry the colour, this carries what the colours say.
    result_summary: str = ""
    # The run's own status in the reader's words, under the strip. Empty for a
    # manifest that could not be read — that cell states the unreadability instead.
    outcome: str = ""
    is_test_run: bool = False
    inputs: list[RunInputCell] = []
    # Caps naming a stage that bound no file — no input line can carry them.
    stage_caps: list[StageRowCap] = []
    # Empty for a run that bound no file — no inputs is not an input set.
    input_key: str = ""
    runs_on_these_inputs: int = 1


# The runs index's three mutually exclusive buckets. Archived takes priority over
# test — archiving is an explicit action that pulls a run off every other list,
# so an archived test run shows only under RUN_VIEW_ARCHIVED.
RUN_VIEW_PRODUCTION = "production"
RUN_VIEW_TEST = "test"
RUN_VIEW_ARCHIVED = "archived"
RUN_VIEWS = (RUN_VIEW_PRODUCTION, RUN_VIEW_TEST, RUN_VIEW_ARCHIVED)
_RUN_VIEW_LABELS = {
    RUN_VIEW_PRODUCTION: "Production",
    RUN_VIEW_TEST: "Test runs",
    RUN_VIEW_ARCHIVED: "Archived",
}


class RunViewChoice(BaseModel):
    view: str
    label: str
    count: int


class RunStatusChoice(BaseModel):
    status: str
    label: str
    count: int


def build_run_index_rows(
    project_id: str, *, view: str | None = None, input_key: str | None = None,
    file_sha256: str | None = None,
) -> list[RunIndexRow]:
    """`view=None` lists every non-archived run; `input_key`/`file_sha256` each narrow it."""
    rows = _build_every_row(project_id, view)
    if input_key:
        rows = [row for row in rows if row.input_key == input_key]
    if file_sha256:
        rows = [row for row in rows if any(i.sha256 == file_sha256 for i in row.inputs)]
    return rows


def count_archived_runs(project_id: str) -> int:
    return len(read_archived_run_ids(project_id))


def build_run_view_choices(project_id: str) -> list[RunViewChoice]:
    counts = _count_runs_by_view(project_id)
    return [
        RunViewChoice(view=view, label=_RUN_VIEW_LABELS[view], count=counts[view])
        for view in RUN_VIEWS
    ]


def build_run_status_choices(rows: list[RunIndexRow]) -> list[RunStatusChoice]:
    """Off the rows on the page, so the list never offers a status this view cannot show."""
    counts = Counter(row.status for row in rows)
    return [RunStatusChoice(status="", label="Any status", count=len(rows))] + [
        RunStatusChoice(status=status, label=describe_run_outcome(status), count=count)
        for status, count in sorted(counts.items())
    ]


def _count_runs_by_view(project_id: str) -> dict[str, int]:
    hidden = read_archived_run_ids(project_id)
    counts = {view: 0 for view in RUN_VIEWS}
    for entry in list_run_entries(project_id):
        counts[_run_view(entry, hidden)] += 1
    return counts


def describe_run_outcome(status: str) -> str:
    return _OUTCOME_WORDS.get(status, status)


def compose_input_key(bindings: list[InputBinding]) -> str:
    """The identity of a run's input SET: which stage read which bytes."""
    if not bindings:
        return ""
    # Hashed where preflight recorded one, path otherwise.
    payload = sorted((b.stage_id, b.sha256 or f"path:{b.path}") for b in bindings)
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


def find_ambiguous_filenames(bindings: Iterable[list[InputBinding]]) -> set[str]:
    """The filenames this project has bound to more than one set of bytes."""
    hashes_by_name: dict[str, set[str]] = defaultdict(set)
    for run_bindings in bindings:
        for binding in run_bindings:
            if binding.sha256:
                hashes_by_name[binding.filename].add(binding.sha256)
    return {name for name, hashes in hashes_by_name.items() if len(hashes) > 1}


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


class _IndexContext(BaseModel):
    project_id: str
    names: dict[str, str]
    ambiguous_filenames: set[str]
    run_counts: Counter[str]
    seen_versions: dict[str, VersionNote] = {}


def _build_every_row(project_id: str, view: str | None) -> list[RunIndexRow]:
    hidden = read_archived_run_ids(project_id)
    entries = [
        entry for entry in reversed(list_run_entries(project_id))
        if _matches_view(entry, hidden, view)
    ]
    bindings = {entry.run_id: read_input_bindings(entry.raw or {}) for entry in entries}
    context = _IndexContext(
        project_id=project_id,
        names=read_run_names(project_id),
        ambiguous_filenames=find_ambiguous_filenames(bindings.values()),
        run_counts=Counter(compose_input_key(b) for b in bindings.values()),
    )
    return [_build_row(entry, bindings[entry.run_id], context) for entry in entries]


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


def _build_row(
    entry: RunEntry, bindings: list[InputBinding], context: _IndexContext
) -> RunIndexRow:
    name = context.names.get(entry.run_id, "")
    if entry.manifest is None:
        # An identity-only row rather than counts it never read, so one unreadable
        # run never takes the index down with it. No test-run filter here on
        # purpose: the index LISTS test runs (flagged), the dashboard count omits them.
        return RunIndexRow(run_id=entry.run_id, status=UNREADABLE_RUN_STATUS, name=name)
    manifest = entry.manifest
    persisted = manifest.to_dict()
    strip = build_stage_strip(persisted)
    input_key = compose_input_key(bindings)
    return RunIndexRow(
        run_id=entry.run_id,
        status=str(manifest.status),
        name=name,
        started_at=manifest.started_at,
        duration=describe_run_duration(persisted),
        cost_usd=total_run_cost(manifest),
        version=_read_version(context, manifest.workflow_version),
        strip=strip,
        result_summary=describe_stage_counts(strip),
        outcome=describe_run_outcome(str(manifest.status)),
        is_test_run=manifest.parameters.is_test_run,
        inputs=[
            _build_input_cell(b, context.ambiguous_filenames, manifest.parameters.limits)
            for b in bindings
        ],
        stage_caps=find_unbound_stage_caps(manifest.parameters.limits, bindings),
        input_key=input_key,
        runs_on_these_inputs=context.run_counts[input_key],
    )


def total_run_cost(manifest: RunManifest) -> float:
    return sum(r.llm_usage.cost_usd for r in manifest.stage_records if r.llm_usage)


def find_unbound_stage_caps(
    limits: dict[str, int], bindings: list[InputBinding]
) -> list[StageRowCap]:
    """A cap on a file input rides that file's line; the rest have nowhere else to go."""
    bound = {binding.stage_id for binding in bindings}
    return [
        StageRowCap(stage_id=stage_id, cap=cap)
        for stage_id, cap in sorted(limits.items())
        if stage_id not in bound
    ]


def _build_input_cell(
    binding: InputBinding, ambiguous: set[str], limits: dict[str, int]
) -> RunInputCell:
    return RunInputCell(
        stage_id=binding.stage_id,
        filename=binding.filename,
        size=describe_bytes(binding.bytes) if binding.bytes is not None else "",
        sha256=binding.sha256,
        hash_disambiguates=bool(binding.sha256) and binding.filename in ambiguous,
        row_cap=limits.get(binding.stage_id),
    )


def _read_version(context: _IndexContext, version_id: str | None) -> VersionNote:
    key = version_id or ""
    if key not in context.seen_versions:
        context.seen_versions[key] = read_version_note(context.project_id, version_id)
    return context.seen_versions[key]
