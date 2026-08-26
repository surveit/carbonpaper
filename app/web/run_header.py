"""The run page's header view-model: the grounding line, the stage strip, and the
one action this run's state calls for. The state is never spelled out in words —
the reader gets it off the CTA (`_run_header.html`)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel

from app.core.errors import RunVersionUnresolvableError
from app.core.json_types import JsonScalar
from app.models.records.workflow_output import WorkflowOutput
from app.core.run_status import RunStatus, StageStatus
from app.services import run as run_service
from app.services.run_manifest_metadata import read_run_name
from app.web.stage_strip import (
    StageStrip,
    StatusCount,
    build_stage_strip,
    count_stage_status,
    read_stage_records,
)

ActionKind = Literal["primary", "ghost"]


class RunAction(BaseModel):
    # `method` picks the markup: "get" renders a link, "post" a one-button form.
    label: str
    url: str
    method: Literal["get", "post"] = "get"
    kind: ActionKind = "primary"


class RunCta(BaseModel):
    primary: RunAction | None = None
    secondary: list[RunAction] = []
    aside: str | None = None


class ArtifactLink(BaseModel):
    name: str
    url: str


class VersionNote(BaseModel):
    """`message` or `error`, never both."""

    version_id: str | None
    message: str | None = None
    error: str | None = None


class RunLiveView(BaseModel):
    duration: str | None
    duration_verb: str
    aside: str | None
    counts: list[StatusCount]


class RestartOffer(BaseModel):
    """`note` says what Restart would do here; with `offered` false it says why not."""

    offered: bool
    note: str


class WorkflowOutputView(BaseModel):
    slug: str
    label: str
    value: str
    primary: bool
    # The row this value was read from, so a reader can open its lineage.
    href: str


class RunHeader(BaseModel):
    run_id: str
    # Empty when unnamed; the heading falls back to the start time.
    name: str
    started_at: str | None
    is_test_run: bool
    version: VersionNote
    strip: StageStrip
    cta: RunCta
    artifacts: list[ArtifactLink]
    live: RunLiveView
    restart: RestartOffer
    workflow_outputs: list[WorkflowOutputView]


def build_run_header(
    project_id: str, run_id: str, run_dir: Path, manifest: Mapping[str, Any]
) -> RunHeader:
    strip = build_stage_strip(manifest)
    cta = choose_run_cta(project_id, run_id, manifest)
    return RunHeader(
        workflow_outputs=read_workflow_outputs(project_id, run_id),
        run_id=run_id,
        name=read_run_name(project_id, run_id),
        started_at=_read_text(manifest.get("started_at")),
        is_test_run=bool(manifest.get("parameters", {}).get("is_test_run")),
        version=read_version_note(project_id, manifest.get("workflow_version")),
        strip=strip,
        cta=cta,
        artifacts=list_artifact_links(project_id, run_id, run_dir, manifest),
        live=_build_live_view(manifest, strip, cta),
        restart=describe_restart(manifest),
    )


def build_live_view(
    project_id: str, run_id: str, manifest: Mapping[str, Any]
) -> RunLiveView:
    return _build_live_view(
        manifest,
        build_stage_strip(manifest),
        choose_run_cta(project_id, run_id, manifest),
    )


def choose_run_cta(
    project_id: str,
    run_id: str,
    manifest: Mapping[str, Any],
) -> RunCta:
    base = f"/project/{project_id}/runs/{run_id}"
    if manifest.get("status") == RunStatus.RUNNING:
        return _build_cancel_cta(base, manifest)
    halted = find_halted_stage_ids(manifest)
    if halted:
        return _build_review_cta(base, manifest, halted)
    if count_stage_status(manifest, StageStatus.ERROR):
        return _build_rerun_cta(base, manifest)
    if manifest.get("status") == RunStatus.CANCELLED:
        return _build_resume_cta(base, manifest)
    # Finished clean: nothing is asked of the reader. Its outputs are not an action
    # and are not rendered as one — they are their own section on the run page.
    return RunCta()


def describe_restart(manifest: Mapping[str, Any]) -> RestartOffer:
    if manifest.get("status") == RunStatus.RUNNING:
        return RestartOffer(offered=True, note=_RESTART_WHILE_RUNNING)
    waiting = count_stages_to_rerun(manifest)
    # No records is not "all completed": a resume walks the version's stages.
    if read_stage_records(manifest) and not waiting:
        return RestartOffer(offered=False, note=_RESTART_HAS_NOTHING_TO_DO)
    done = _count_completed(manifest)
    stages = f"{waiting} stage{'' if waiting == 1 else 's'}"
    if not done:
        return RestartOffer(offered=True,
                            note=f"Runs all {stages} — none of them completed.")
    return RestartOffer(offered=True, note=(
        f"Runs the {stages} that have not completed and reuses the {done} that "
        f"{'has' if done == 1 else 'have'}."
    ))


def count_stages_to_rerun(manifest: Mapping[str, Any]) -> int:
    return len(read_stage_records(manifest)) - _count_completed(manifest)


def find_halted_stage_ids(manifest: Mapping[str, Any]) -> list[str]:
    halted = manifest.get("halted_at") or []
    if halted:
        return [str(stage_id) for stage_id in halted]
    return [
        str(record.get("stage_id"))
        for record in read_stage_records(manifest)
        if record.get("status") == StageStatus.AWAITING_REVIEW
    ]


def read_version_note(project_id: str, version_id: object) -> VersionNote:
    text = _read_text(version_id)
    if text is None:
        return VersionNote(version_id=None)
    try:
        version = run_service.load_run_version(project_id, {"workflow_version": text})
    except RunVersionUnresolvableError as exc:
        return VersionNote(version_id=text, error=str(exc))
    return VersionNote(version_id=text, message=version.message or None)


def list_artifact_links(
    project_id: str, run_id: str, run_dir: Path, manifest: Mapping[str, Any]
) -> list[ArtifactLink]:
    if manifest.get("status") in (RunStatus.RUNNING, None):
        return []
    has_ok_publish = any(
        record.get("type") == "publish"
        and record.get("status") in (StageStatus.OK, StageStatus.VALIDATION_WARNINGS)
        for record in read_stage_records(manifest)
    )
    artifacts_root = run_dir / "artifacts"
    if not (has_ok_publish and artifacts_root.is_dir()):
        return []
    files = sorted(f for f in artifacts_root.rglob("*") if f.is_file())
    index = next((f for f in files if f.name == "index.html"), None)
    if index is not None:
        files = [index]
    return [
        ArtifactLink(
            name=f.name,
            url=(f"/project/{project_id}/runs/{run_id}/artifact/"
                 f"{f.relative_to(artifacts_root).as_posix()}"),
        )
        for f in files
    ]


def measure_elapsed_seconds(
    started_at: str | None, finished_at: str | None, *, still_running: bool
) -> float | None:
    start = read_timestamp(started_at)
    if start is None:
        return None
    if finished_at:
        end = read_timestamp(finished_at)
    else:
        end = datetime.now(tz=start.tzinfo) if still_running else None
    if end is None:
        return None
    # One stored timestamp carrying an offset and the other not leaves the naive one's
    # offset unknown, and assuming it would invent the duration.
    if (start.tzinfo is None) != (end.tzinfo is None):
        return None
    seconds = (end - start).total_seconds()
    return seconds if seconds >= 0 else None


def format_duration(seconds: float) -> str:
    whole = int(seconds)
    if whole < _SECONDS_PER_MINUTE:
        return f"{whole}s"
    if whole < _SECONDS_PER_HOUR:
        return f"{whole // _SECONDS_PER_MINUTE}m {whole % _SECONDS_PER_MINUTE:02d}s"
    minutes = (whole % _SECONDS_PER_HOUR) // _SECONDS_PER_MINUTE
    return f"{whole // _SECONDS_PER_HOUR}h {minutes:02d}m"


def describe_run_duration(manifest: Mapping[str, Any]) -> str | None:
    seconds = measure_elapsed_seconds(
        _read_text(manifest.get("started_at")),
        _read_text(manifest.get("finished_at")),
        still_running=manifest.get("status") == RunStatus.RUNNING,
    )
    return None if seconds is None else format_duration(seconds)


def read_file_name(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


# ─── The CTA per run state ───────────────────────────────────────────────────

# Both re-run paths POST the same /resume route, which re-runs every stage that
# is not already complete and reuses the completed ones' outputs.
_CACHE_NOTE = "keeps the {done} completed stage{s} — no new LLM calls"


def _build_live_view(
    manifest: Mapping[str, Any], strip: StageStrip, cta: RunCta
) -> RunLiveView:
    running = manifest.get("status") == RunStatus.RUNNING
    return RunLiveView(
        duration=describe_run_duration(manifest),
        duration_verb="running" if running else "ran",
        aside=cta.aside,
        counts=strip.counts,
    )


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
    errors = count_stage_status(manifest, StageStatus.ERROR)
    if errors:
        secondary.append(
            _build_resume_action(_describe_failed(errors), kind="ghost", base=base)
        )
    return RunCta(
        primary=RunAction(label=_describe_review(manifest, first),
                          url=f"{base}/queue/{first}"),
        secondary=secondary,
        aside=_describe_what_the_review_releases(manifest, len(halted)),
    )


def _build_rerun_cta(base: str, manifest: Mapping[str, Any]) -> RunCta:
    errors = count_stage_status(manifest, StageStatus.ERROR)
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


def _build_resume_action(label: str, *, kind: ActionKind, base: str) -> RunAction:
    return RunAction(label=label, url=f"{base}/resume", method="post", kind=kind)


def _describe_failed(errors: int) -> str:
    return f"↻ Re-run {errors} failed stage{'' if errors == 1 else 's'} →"


def _describe_review(manifest: Mapping[str, Any], stage_id: str) -> str:
    stats = manifest.get("human_review_queue_stats") or {}
    pending = (stats.get(stage_id) or {}).get("items_pending")
    if pending is None:
        return f"👤 Review items in {stage_id} →"
    return f"👤 Review {pending} item{'' if pending == 1 else 's'} in {stage_id} →"


def _describe_what_the_review_releases(
    manifest: Mapping[str, Any], halted_count: int
) -> str | None:
    waiting = count_stage_status(manifest, StageStatus.PENDING)
    if not waiting:
        return None
    subject = "this is" if halted_count == 1 else "these are"
    plural = "" if waiting == 1 else "s"
    return f"{waiting} stage{plural} run{'s' if waiting == 1 else ''} once {subject} decided"


_RESTART_WHILE_RUNNING = (
    "Runs every stage that has not completed. This run's status says it is still "
    "executing, and nothing here checks whether that is true: if it is, both "
    "executors write this run's stage records and the last write wins."
)

_RESTART_HAS_NOTHING_TO_DO = (
    "Every stage completed, so a restart would run none of them. Duplicate run "
    "opens the run form on this run's version, files and row limits."
)


def _count_completed(manifest: Mapping[str, Any]) -> int:
    return count_stage_status(manifest, StageStatus.OK) + count_stage_status(
        manifest, StageStatus.VALIDATION_WARNINGS
    )


def _describe_cache_reuse(manifest: Mapping[str, Any]) -> str:
    done = _count_completed(manifest)
    return _CACHE_NOTE.format(done=done, s="" if done == 1 else "s")


def _describe_running_stage(manifest: Mapping[str, Any]) -> str | None:
    records = read_stage_records(manifest)
    for position, record in enumerate(records, start=1):
        if record.get("status") == StageStatus.RUNNING:
            return f"on {record.get('stage_id')} — stage {position} of {len(records)}"
    return None


# ─── Manifest reading ────────────────────────────────────────────────────────

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def _read_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def read_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def read_workflow_outputs(project_id: str, run_id: str) -> list[WorkflowOutputView]:
    """Filtered in python: a run id sits inside the citation, which find() cannot select on."""
    published = [o for o in WorkflowOutput.list() if o.citation.run_id == run_id]
    return [
        WorkflowOutputView(
            slug=output.slug,
            label=output.label,
            primary=output.primary,
            value=render_output_value(output.citation.value),
            href=run_service.build_row_trace_url(
                project_id, run_id, output.citation.stage_id, output.citation.row_ordinal,
                column=output.citation.column,
            ),
        )
        for output in sorted(published, key=lambda o: o.slug)
    ]


def render_output_value(value: JsonScalar) -> str:
    """A null reads as absent rather than as the word None."""
    return "—" if value is None else f"{value:,}" if isinstance(value, (int, float)) else str(value)
