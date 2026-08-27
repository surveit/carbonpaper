"""What the packet index leads with, for a reader who has never used this app."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from app.core.run_status import StageStatus
from app.models.stages.stage_base import StageType
from app.services.methodology import read_methodology_headline
from app.services.project_record import read_project_name
from app.services.review_packet.checksums import CHECKSUMS_FILE
from app.services.review_packet.data import (
    DATA_DIR,
    DOCUMENT_FILE,
    WORKFLOW_FILE,
    DataReport,
)
from app.services.review_packet.views import LineageReport, RunView, StageView
from app.web.file_sizes import describe_bytes
from app.web.published_files import PublishedFile
from app.web.panel_links import PacketPanelLinks
from app.web.published_claims import PublishedClaims
from app.web.run_header import format_duration, measure_elapsed_seconds, read_timestamp
from app.web.run_issues import RunIssues

# Spelled out as its own link text, so a folder opened offline still reads an address.
CARBON_PAPER_URL = "https://github.com/surveit/carbonpaper"

ISSUES_ANCHOR = "#run-issues"
INPUTS_ANCHOR = "#packet-inputs"
PROVENANCE_ANCHOR = "#packet-provenance"
WORKFLOW_ANCHOR = "#packet-workflow"


class PacketLink(BaseModel):
    text: str
    href: str
    # A stage id, which the app sets in mono wherever it prints one.
    is_stage_id: bool = False


class PacketHeadline(BaseModel):
    """`title` is the method's own first heading, else the project's name."""

    title: str
    standfirst: str | None
    started_at: str | None
    duration: str | None
    step_count: int
    status: str
    project_id: str
    run_id: str
    version_id: str | None


class PacketCheck(BaseModel):
    lead: str
    text: str
    links: list[PacketLink] = []


class PacketColophon(BaseModel):
    """A `…_href` is None where the packet could not write that file."""

    written_at: str
    project_id: str
    run_id: str
    version_id: str | None
    document_href: str | None
    workflow_href: str | None
    source_url: str = CARBON_PAPER_URL


class PacketFrontPage(BaseModel):
    headline: PacketHeadline
    claims: PublishedClaims
    files: list[PublishedFile]
    checks: list[PacketCheck]
    colophon: PacketColophon


def build_packet_front_page(
    project_id: str,
    root: Path,
    view: RunView,
    data: DataReport,
    lineage: LineageReport,
    issues: RunIssues,
    claims: PublishedClaims,
) -> PacketFrontPage:
    written = frozenset(data.written)
    return PacketFrontPage(
        headline=build_packet_headline(project_id, view),
        claims=claims,
        files=[_read_published_file(root, path) for path in data.artifacts],
        checks=find_packet_checks(view, lineage, issues, written),
        colophon=build_packet_colophon(view, written),
    )


def build_packet_headline(project_id: str, view: RunView) -> PacketHeadline:
    headline = read_methodology_headline(project_id)
    return PacketHeadline(
        title=headline.title or read_project_name(project_id),
        standfirst=headline.standfirst,
        started_at=_describe_moment(read_timestamp(view.started_at or None)),
        duration=_read_duration(view),
        step_count=len(view.stages),
        status=view.status,
        project_id=view.project or project_id,
        run_id=view.run_id,
        version_id=view.workflow_version,
    )


def build_packet_colophon(view: RunView, written: frozenset[str]) -> PacketColophon:
    now = datetime.now(tz=timezone.utc)
    return PacketColophon(
        written_at=f"{_describe_moment(now)} UTC",
        project_id=view.project,
        run_id=view.run_id,
        version_id=view.workflow_version,
        document_href=DOCUMENT_FILE if DOCUMENT_FILE in written else None,
        workflow_href=WORKFLOW_FILE if WORKFLOW_FILE in written else None,
    )


def find_packet_checks(
    view: RunView,
    lineage: LineageReport,
    issues: RunIssues,
    written: frozenset[str],
) -> list[PacketCheck]:
    found = (
        _count_the_method(written),
        _count_the_traces(lineage),
        _count_the_sources(view),
        _count_the_judgement(view),
        _count_what_was_flagged(issues),
        _count_what_recomputes_it(),
    )
    return [check for check in found if check is not None]


def _count_the_method(written: frozenset[str]) -> PacketCheck | None:
    if DOCUMENT_FILE not in written:
        return None
    return PacketCheck(
        lead="Method",
        text="the prose this workflow was compiled from, as the run started.",
        links=[PacketLink(text=DOCUMENT_FILE, href=DOCUMENT_FILE)],
    )


def _count_the_traces(lineage: LineageReport) -> PacketCheck | None:
    if not lineage.figures:
        return None
    total = len(lineage.figures)
    traced = sum(1 for figure in lineage.figures if figure.href)
    return PacketCheck(
        lead="Figures",
        text=f"{_count(total, 'figure')} published, {traced} traced to their rows here.",
        links=[PacketLink(text="Row provenance", href=PROVENANCE_ANCHOR)],
    )


def _count_the_sources(view: RunView) -> PacketCheck | None:
    if not view.inputs:
        return None
    return PacketCheck(
        lead="Sources",
        text=(
            f"{_count(len(view.inputs), 'file')} read, each copied into this folder "
            "beside the SHA-256 the run recorded as it read it."
        ),
        links=[PacketLink(text="Inputs this run read", href=INPUTS_ANCHOR)],
    )


def _count_the_judgement(view: RunView) -> PacketCheck:
    asked = _find_stages_of_type(view, StageType.llm_transform)
    queued = _find_stages_of_type(view, StageType.human_review_queue)
    links = PacketPanelLinks(to_root="")
    return PacketCheck(
        lead="Judgement",
        text=_say_where_judgement_entered(asked, queued),
        links=[
            *(
                PacketLink(
                    text=stage.stage_id,
                    href=links.stage_anchor(stage.stage_id),
                    is_stage_id=True,
                )
                for stage in [*asked, *queued]
            ),
            PacketLink(text="The workflow this run executed", href=WORKFLOW_ANCHOR),
        ],
    )


def _say_where_judgement_entered(asked: list[StageView], queued: list[StageView]) -> str:
    counted = [
        part
        for part in (_describe_model_steps(asked), _describe_review_steps(queued))
        if part
    ]
    if not counted:
        return "no step called an AI model, and no step put a row to a person."
    return "; ".join(counted) + "; no other step asked a model or a person anything."


def _describe_model_steps(stages: list[StageView]) -> str:
    if not stages:
        return ""
    rows = sum(stage.row_count for stage in stages)
    return f"{_count(len(stages), 'step')} asked an AI model over {_count(rows, 'row')}"


def _describe_review_steps(stages: list[StageView]) -> str:
    if not stages:
        return ""
    rows = sum(stage.row_count for stage in stages)
    return f"{_count(len(stages), 'step')} put {_count(rows, 'row')} to a person"


def _count_what_was_flagged(issues: RunIssues) -> PacketCheck | None:
    counted = ((issues.error_count, "error"), (issues.warning_count, "warning"))
    named = [_count(total, noun) for total, noun in counted if total]
    if not named:
        return None
    return PacketCheck(
        lead="Flagged",
        text=" and ".join(named) + ", each naming the step it came from.",
        links=[PacketLink(text="What the run flagged", href=ISSUES_ANCHOR)],
    )


def _count_what_recomputes_it() -> PacketCheck:
    return PacketCheck(
        lead="Recompute",
        text=f"one uncapped CSV per step in {DATA_DIR}/, and a hash for every file here.",
        links=[PacketLink(text=CHECKSUMS_FILE, href=CHECKSUMS_FILE)],
    )


def _read_published_file(root: Path, path: str) -> PublishedFile:
    name = path.rsplit("/", 1)[-1]
    folder = path[: len(path) - len(name)]
    size = describe_bytes((root / path).stat().st_size)
    return PublishedFile(
        name=name, href=path, note=f"{size} · {folder}" if folder else size
    )


def _find_stages_of_type(view: RunView, stage_type: StageType) -> list[StageView]:
    return [
        stage
        for stage in view.stages
        if stage.type == stage_type.value and stage.status != StageStatus.PENDING
    ]


def _read_duration(view: RunView) -> str | None:
    seconds = measure_elapsed_seconds(
        view.started_at or None, view.finished_at, still_running=False
    )
    return None if seconds is None else format_duration(seconds)


def _describe_moment(stamp: datetime | None) -> str | None:
    return None if stamp is None else f"{stamp.day} {stamp:%B %Y} at {stamp:%H:%M}"


def _count(total: int, noun: str) -> str:
    return f"{total:,} {noun}{'' if total == 1 else 's'}"
