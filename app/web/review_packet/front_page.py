"""The packet index's front matter, for a reader who has never used this app: what
was asked, what was published, and the counted places a sceptic should look."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.core.run_status import StageStatus
from app.models.stages.stage_base import StageType
from app.services.methodology import read_methodology_headline
from app.services.project_record import read_project_name
from app.services.review_packet.checksums import CHECKSUMS_FILE
from app.services.review_packet.data import DATA_DIR, DOCUMENT_FILE
from app.services.review_packet.views import LineageReport, RunView
from app.web.file_sizes import describe_bytes
from app.web.review_packet.claims import PacketClaims
from app.web.run_header import format_duration, measure_elapsed_seconds
from app.web.run_issues import RunIssues

# The one address a packet carries that is not a file beside it. Written as its own
# link text, so a folder opened with no network still reads it as an address.
CARBON_PAPER_URL = "https://github.com/surveit/carbonpaper"

ISSUES_ANCHOR = "#run-issues"
INPUTS_ANCHOR = "#packet-inputs"
PROVENANCE_ANCHOR = "#packet-provenance"
WORKFLOW_ANCHOR = "#packet-workflow"


class PacketLink(BaseModel):
    text: str
    href: str


class PacketHeadline(BaseModel):
    """`title` is the method's own first heading where it has one, else the project's name."""

    title: str
    standfirst: str | None
    project_name: str
    started_at: str
    duration: str | None
    step_count: int
    status: str
    version_id: str | None


class PublishedFile(BaseModel):
    name: str
    folder: str
    size: str
    href: str


class PacketCheck(BaseModel):
    lead: str
    text: str
    links: list[PacketLink] = []


class PacketColophon(BaseModel):
    written_at: str
    source_url: str = CARBON_PAPER_URL


class PacketFrontPage(BaseModel):
    headline: PacketHeadline
    claims: PacketClaims
    files: list[PublishedFile]
    checks: list[PacketCheck]
    colophon: PacketColophon


def build_packet_front_page(
    project_id: str,
    root: Path,
    view: RunView,
    artifacts: list[str],
    lineage: LineageReport,
    issues: RunIssues,
    claims: PacketClaims,
    has_guide: bool,
    written_at: str,
) -> PacketFrontPage:
    return PacketFrontPage(
        headline=build_packet_headline(project_id, view),
        claims=claims,
        files=list_published_files(root, artifacts),
        checks=find_packet_checks(view, lineage, issues, has_guide),
        colophon=PacketColophon(written_at=written_at),
    )


def build_packet_headline(project_id: str, view: RunView) -> PacketHeadline:
    project_name = read_project_name(project_id)
    headline = read_methodology_headline(project_id)
    return PacketHeadline(
        title=headline.title or project_name,
        standfirst=headline.standfirst,
        project_name=project_name,
        started_at=view.started_at,
        duration=_read_duration(view),
        step_count=len(view.stages),
        status=view.status,
        version_id=view.workflow_version,
    )


def list_published_files(root: Path, artifacts: list[str]) -> list[PublishedFile]:
    return [_read_published_file(root, path) for path in artifacts]


def find_packet_checks(
    view: RunView, lineage: LineageReport, issues: RunIssues, has_guide: bool
) -> list[PacketCheck]:
    checks = [_describe_the_method(has_guide)]
    traces = _describe_the_traces(lineage)
    if traces is not None:
        checks.append(traces)
    sources = _describe_the_sources(view)
    if sources is not None:
        checks.append(sources)
    checks.append(_describe_the_judgement(view))
    flagged = _describe_what_was_flagged(issues)
    if flagged is not None:
        checks.append(flagged)
    checks.append(_describe_recomputing_it())
    return checks


def _describe_the_method(has_guide: bool) -> PacketCheck:
    walkthrough = (
        " The run walkthrough beside this page is its author saying which steps are "
        "worth scrutinising."
        if has_guide
        else ""
    )
    return PacketCheck(
        lead="Read the method.",
        text=(
            "The prose this workflow was compiled from, as it stood when the run "
            f"started.{walkthrough}"
        ),
        links=[PacketLink(text=DOCUMENT_FILE, href=DOCUMENT_FILE)],
    )


def _describe_the_traces(lineage: LineageReport) -> PacketCheck | None:
    if not lineage.figures:
        return None
    total = len(lineage.figures)
    traced = sum(1 for figure in lineage.figures if figure.href)
    return PacketCheck(
        lead="Follow a figure back to its rows.",
        text=(
            f"{_count(total, 'figure')} published, "
            + (
                "and every one carries a row-by-row trace in this folder."
                if traced == total
                else f"{traced} of them carrying a row-by-row trace in this folder."
            )
        ),
        links=[PacketLink(text="Row provenance", href=PROVENANCE_ANCHOR)],
    )


def _describe_the_sources(view: RunView) -> PacketCheck | None:
    if not view.inputs:
        return None
    return PacketCheck(
        lead="Check the sources.",
        text=(
            f"This run read {_count(len(view.inputs), 'file')}. Each is copied into "
            "this folder beside the SHA-256 the run recorded as it read it."
        ),
        links=[PacketLink(text="Inputs this run read", href=INPUTS_ANCHOR)],
    )


def _describe_the_judgement(view: RunView) -> PacketCheck:
    asked = _find_stages_of_type(view, StageType.llm_transform)
    queued = _find_stages_of_type(view, StageType.human_review_queue)
    if not asked and not queued:
        text = (
            "No step in this run called an AI model, and no step put a row to a "
            "person. Every step is code, and its code is on the step's own page."
        )
    else:
        text = " ".join(
            part
            for part in (
                _describe_model_steps(asked),
                _describe_review_steps(queued),
                "No other step asked anything of a model or a person.",
            )
            if part
        )
    return PacketCheck(
        lead="See where judgement entered.",
        text=text,
        links=[
            *(PacketLink(text=stage.stage_id, href=_stage_href(stage.stage_id))
              for stage in [*asked, *queued]),
            PacketLink(text="The workflow this run executed", href=WORKFLOW_ANCHOR),
        ],
    )


def _describe_model_steps(stages: list) -> str:
    if not stages:
        return ""
    rows = sum(stage.row_count for stage in stages)
    return f"{_count(len(stages), 'step')} asked an AI model, over {_count(rows, 'row')}."


def _describe_review_steps(stages: list) -> str:
    if not stages:
        return ""
    rows = sum(stage.row_count for stage in stages)
    return f"{_count(len(stages), 'step')} put {_count(rows, 'row')} to a person."


def _describe_what_was_flagged(issues: RunIssues) -> PacketCheck | None:
    counted = [
        _count(issues.error_count, "error"),
        _count(issues.warning_count, "warning"),
    ]
    named = [text for text, total in zip(counted, (issues.error_count,
                                                  issues.warning_count)) if total]
    if not named:
        return None
    return PacketCheck(
        lead="Read what the run flagged about itself.",
        text=" and ".join(named) + ", each naming the step it came from.",
        links=[PacketLink(text="What the run flagged", href=ISSUES_ANCHOR)],
    )


def _describe_recomputing_it() -> PacketCheck:
    return PacketCheck(
        lead="Recompute it.",
        text=(
            f"{DATA_DIR}/ holds one uncapped CSV per step, and {CHECKSUMS_FILE} "
            "checks every file in this folder against the bytes that were written."
        ),
        links=[PacketLink(text=CHECKSUMS_FILE, href=CHECKSUMS_FILE)],
    )


def _read_published_file(root: Path, path: str) -> PublishedFile:
    name = path.rsplit("/", 1)[-1]
    written = root / path
    return PublishedFile(
        name=name,
        folder=path[: len(path) - len(name)],
        size=describe_bytes(written.stat().st_size),
        href=path,
    )


def _find_stages_of_type(view: RunView, stage_type: StageType) -> list:
    return [
        stage
        for stage in view.stages
        if stage.type == stage_type.value and stage.status != StageStatus.PENDING
    ]


def _stage_href(stage_id: str) -> str:
    from app.web.review_packet.pages import STAGES_DIR

    return f"{STAGES_DIR}/{stage_id}.html"


def _read_duration(view: RunView) -> str | None:
    seconds = measure_elapsed_seconds(
        view.started_at or None, view.finished_at, still_running=False
    )
    return None if seconds is None else format_duration(seconds)


def _count(total: int, noun: str) -> str:
    return f"{total:,} {noun}{'' if total == 1 else 's'}"
