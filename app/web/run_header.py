"""The run header's view-model: grounding line, the one primary action, and the
stage strip. The strip is shared with the runs index (`_stage_strip.html`)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel

from app.core.errors import RunVersionUnresolvableError
from app.core.run_status import RunStatus, StageStatus
from app.services import run as run_service


class StageSquare(BaseModel):
    """One stage's square in the strip. `status` is a StageStatus value, used
    verbatim as the `status-<value>` CSS class."""

    stage_id: str
    status: str


class StatusTally(BaseModel):
    """One labelled count beneath the strip, e.g. 4 stages "blocked behind it"."""

    status: str
    label: str
    count: int


class StageStrip(BaseModel):
    squares: list[StageSquare]
    tallies: list[StatusTally]


ActionKind = Literal["primary", "ghost"]


class RunAction(BaseModel):
    """One button in the CTA row. `method` picks the markup: "get" renders a
    link, "post" a single-button form."""

    label: str
    url: str
    method: Literal["get", "post"] = "get"
    kind: ActionKind = "primary"


class RunCta(BaseModel):
    """The action row. `primary` is None only for a finished run that published
    nothing — there is then no action to offer."""

    primary: RunAction | None = None
    secondary: list[RunAction] = []
    aside: str | None = None


class ArtifactLink(BaseModel):
    name: str
    url: str


class VersionNote(BaseModel):
    """The pinned version as the header states it — `message` when the version
    resolved, `error` when it could not be read, never both."""

    version_id: str | None
    message: str | None = None
    error: str | None = None


class RunLiveView(BaseModel):
    """The header parts the run page's 2s poller refreshes in place."""

    duration: str | None
    duration_verb: str
    aside: str | None
    tallies: list[StatusTally]


class RunHeader(BaseModel):
    run_id: str
    started_at: str | None
    is_test_run: bool
    version: VersionNote
    strip: StageStrip
    cta: RunCta
    live: RunLiveView


def build_run_header(
    project: str,
    run_id: str,
    manifest: Mapping[str, Any],
    artifacts: Sequence[ArtifactLink],
) -> RunHeader:
    strip = build_stage_strip(manifest)
    cta = choose_run_cta(project, run_id, manifest, artifacts)
    return RunHeader(
        run_id=run_id,
        started_at=_read_text(manifest.get("started_at")),
        is_test_run=bool(manifest.get("is_test_run")),
        version=read_version_note(project, manifest),
        strip=strip,
        cta=cta,
        live=_build_live_view(manifest, strip, cta),
    )


def build_live_view(
    project: str, run_id: str, manifest: Mapping[str, Any]
) -> RunLiveView:
    """What the run page's poller refreshes. Artifacts are left out: a run only
    publishes them once it has finished, at which point the page reloads."""
    return _build_live_view(
        manifest,
        build_stage_strip(manifest),
        choose_run_cta(project, run_id, manifest, []),
    )


def _build_live_view(
    manifest: Mapping[str, Any], strip: StageStrip, cta: RunCta
) -> RunLiveView:
    running = manifest.get("status") == RunStatus.RUNNING
    seconds = measure_elapsed_seconds(
        _read_text(manifest.get("started_at")),
        _read_text(manifest.get("finished_at")),
        still_running=running,
    )
    return RunLiveView(
        duration=format_duration(seconds) if seconds is not None else None,
        duration_verb="running" if running else "ran",
        aside=cta.aside,
        tallies=strip.tallies,
    )


def build_stage_strip(manifest: Mapping[str, Any]) -> StageStrip:
    """One square per stage in the manifest's own (topological) order, plus a
    labelled count per status actually present."""
    records = _read_records(manifest)
    squares = [
        StageSquare(stage_id=str(r.get("stage_id", "")), status=str(r.get("status", "")))
        for r in records
    ]
    return StageStrip(
        squares=squares,
        tallies=_build_tallies(squares, _read_text(manifest.get("status"))),
    )


def choose_run_cta(
    project: str,
    run_id: str,
    manifest: Mapping[str, Any],
    artifacts: Sequence[ArtifactLink],
) -> RunCta:
    """The single action this run's state calls for — the page states no status
    headline, so this button is how the state is read."""
    base = f"/project/{project}/runs/{run_id}"
    if manifest.get("status") == RunStatus.RUNNING:
        return _build_cancel_cta(base, manifest)
    halted = find_halted_stage_ids(manifest)
    if halted:
        return _build_review_cta(base, manifest, halted)
    if _count_status(manifest, StageStatus.ERROR):
        return _build_rerun_cta(base, manifest)
    if manifest.get("status") == RunStatus.CANCELLED:
        return _build_resume_cta(base, manifest)
    return _build_artifacts_cta(artifacts)


def find_halted_stage_ids(manifest: Mapping[str, Any]) -> list[str]:
    """The stages holding the run open for review: `halted_at` when the run
    recorded one, otherwise every stage sitting in awaiting_review."""
    halted = manifest.get("halted_at") or []
    if halted:
        return [str(stage_id) for stage_id in halted]
    return [
        str(r.get("stage_id"))
        for r in _read_records(manifest)
        if r.get("status") == StageStatus.AWAITING_REVIEW
    ]


def read_version_note(project: str, manifest: Mapping[str, Any]) -> VersionNote:
    """The pinned version's human-meaningful `message`, or the stated reason the
    version could not be read. Never an empty message standing in for a real one."""
    version_id = _read_text(manifest.get("workflow_version"))
    if version_id is None:
        return VersionNote(version_id=None)
    try:
        version = run_service.load_run_version(project, dict(manifest))
    except RunVersionUnresolvableError as exc:
        return VersionNote(version_id=version_id, error=str(exc))
    return VersionNote(version_id=version_id, message=version.message or None)


def measure_elapsed_seconds(
    started_at: str | None, finished_at: str | None, *, still_running: bool
) -> float | None:
    """How long the run has taken, or None when that cannot be read off the two
    timestamps — a finished run missing `finished_at` gets no duration, not a guess."""
    start = _read_timestamp(started_at)
    if start is None:
        return None
    end = _read_timestamp(finished_at) if finished_at else (
        datetime.now() if still_running else None
    )
    if end is None:
        return None
    seconds = (end - start).total_seconds()
    return seconds if seconds >= 0 else None


def format_duration(seconds: float) -> str:
    """A run length as "48s", "2m 14s" or "1h 04m"."""
    whole = int(seconds)
    if whole < _SECONDS_PER_MINUTE:
        return f"{whole}s"
    if whole < _SECONDS_PER_HOUR:
        return f"{whole // _SECONDS_PER_MINUTE}m {whole % _SECONDS_PER_MINUTE:02d}s"
    minutes = (whole % _SECONDS_PER_HOUR) // _SECONDS_PER_MINUTE
    return f"{whole // _SECONDS_PER_HOUR}h {minutes:02d}m"


def read_file_name(path: str) -> str:
    """The last segment of a path recorded on either platform's separator."""
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


# ─── The CTA per run state ───────────────────────────────────────────────────

# Both re-run paths POST the same /resume route, which re-runs every stage that
# is not already complete and reuses the completed ones' outputs.
_CACHE_NOTE = "keeps the {done} completed stage{s} — no new LLM calls"


def _build_cancel_cta(base: str, manifest: Mapping[str, Any]) -> RunCta:
    return RunCta(
        primary=RunAction(
            label="✕ Cancel run", url=f"{base}/cancel", method="post", kind="ghost"
        ),
        aside=_describe_running_stage(manifest),
    )


def _build_review_cta(
    base: str, manifest: Mapping[str, Any], halted: list[str]
) -> RunCta:
    first, rest = halted[0], halted[1:]
    secondary = [
        RunAction(label=f"👤 Review {stage_id} →", url=f"{base}/queue/{stage_id}",
                  kind="ghost")
        for stage_id in rest
    ]
    errors = _count_status(manifest, StageStatus.ERROR)
    if errors:
        secondary.append(_build_resume_action(_describe_failed(errors), kind="ghost",
                                              base=base))
    return RunCta(
        primary=RunAction(label=_describe_review(manifest, first),
                          url=f"{base}/queue/{first}"),
        secondary=secondary,
        aside=_describe_blocked(manifest, len(halted)),
    )


def _build_rerun_cta(base: str, manifest: Mapping[str, Any]) -> RunCta:
    errors = _count_status(manifest, StageStatus.ERROR)
    return RunCta(
        primary=_build_resume_action(_describe_failed(errors), kind="primary", base=base),
        aside=_describe_cache_reuse(manifest),
    )


def _build_resume_cta(base: str, manifest: Mapping[str, Any]) -> RunCta:
    return RunCta(
        primary=_build_resume_action("↻ Resume cancelled run →", kind="primary",
                                     base=base),
        aside=_describe_cache_reuse(manifest),
    )


def _build_artifacts_cta(artifacts: Sequence[ArtifactLink]) -> RunCta:
    """A finished run has no imperative button — its outputs are the only ones."""
    if not artifacts:
        return RunCta()
    first, *rest = artifacts
    return RunCta(
        primary=RunAction(label=f"📤 {first.name}", url=first.url),
        secondary=[RunAction(label=a.name, url=a.url, kind="ghost") for a in rest],
    )


def _build_resume_action(label: str, *, kind: ActionKind, base: str) -> RunAction:
    return RunAction(label=label, url=f"{base}/resume", method="post", kind=kind)


def _describe_failed(errors: int) -> str:
    return f"↻ Re-run {errors} failed stage{'' if errors == 1 else 's'} →"


def _describe_review(manifest: Mapping[str, Any], stage_id: str) -> str:
    """"Review 40 items in <stage>" — or no count when the run recorded none."""
    stats = manifest.get("human_review_queue_stats") or {}
    pending = (stats.get(stage_id) or {}).get("items_pending")
    if pending is None:
        return f"👤 Review items in {stage_id} →"
    return f"👤 Review {pending} item{'' if pending == 1 else 's'} in {stage_id} →"


def _describe_blocked(manifest: Mapping[str, Any], halted_count: int) -> str | None:
    waiting = _count_status(manifest, StageStatus.PENDING)
    if not waiting:
        return None
    these = "this" if halted_count == 1 else "these"
    return f"{waiting} stage{'' if waiting == 1 else 's'} are waiting on {these}"


def _describe_cache_reuse(manifest: Mapping[str, Any]) -> str:
    done = _count_status(manifest, StageStatus.OK) + _count_status(
        manifest, StageStatus.VALIDATION_WARNINGS
    )
    return _CACHE_NOTE.format(done=done, s="" if done == 1 else "s")


def _describe_running_stage(manifest: Mapping[str, Any]) -> str | None:
    records = _read_records(manifest)
    for position, record in enumerate(records, start=1):
        if record.get("status") == StageStatus.RUNNING:
            return (f"on {record.get('stage_id')} — stage {position} "
                    f"of {len(records)}")
    return None


# ─── Tallies ─────────────────────────────────────────────────────────────────

_STATUS_LABEL = {
    StageStatus.OK: "done",
    StageStatus.VALIDATION_WARNINGS: "with warnings",
    StageStatus.RUNNING: "running",
    StageStatus.AWAITING_REVIEW: "waiting on you",
    StageStatus.ERROR: "failed",
    StageStatus.CANCELLED: "cancelled",
}
# Why the remaining stages have not run depends on what stopped the run, so the
# pending label is read off the run's own status rather than fixed.
_PENDING_LABEL = {
    RunStatus.RUNNING: "still to do",
    RunStatus.AWAITING_REVIEW: "blocked behind it",
    RunStatus.ERRORS: "not reached",
}
_PENDING_LABEL_OTHERWISE = "never ran"
# Display order for the counts beneath the strip: what finished, then what is in
# flight, then what needs a human, then what went wrong, then what never ran.
_TALLY_ORDER = (
    StageStatus.OK,
    StageStatus.VALIDATION_WARNINGS,
    StageStatus.RUNNING,
    StageStatus.AWAITING_REVIEW,
    StageStatus.ERROR,
    StageStatus.CANCELLED,
    StageStatus.PENDING,
)

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def _build_tallies(
    squares: Sequence[StageSquare], run_status: str | None
) -> list[StatusTally]:
    counts = Counter(square.status for square in squares)
    return [
        StatusTally(status=str(status), count=counts[str(status)],
                    label=_read_tally_label(status, run_status))
        for status in _TALLY_ORDER
        if counts[str(status)]
    ]


def _read_tally_label(status: StageStatus, run_status: str | None) -> str:
    """A pending stage's label says why it has not run, which depends on what
    stopped the run; every other status labels itself."""
    if status is not StageStatus.PENDING:
        return _STATUS_LABEL[status]
    stopped_by = next((s for s in _PENDING_LABEL if s == run_status), None)
    if stopped_by is None:
        return _PENDING_LABEL_OTHERWISE
    return _PENDING_LABEL[stopped_by]


# ─── Manifest reading ────────────────────────────────────────────────────────


def _read_records(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(manifest.get("stage_records") or [])


def _count_status(manifest: Mapping[str, Any], status: StageStatus) -> int:
    return sum(1 for r in _read_records(manifest) if r.get("status") == status)


def _read_text(value: object) -> str | None:
    """A manifest field as a non-empty string, or None — an absent value stays
    absent rather than becoming ""."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
