"""The packet's data half: the run's records and outputs, pinned method, sources and spans."""
from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from itertools import islice
from pathlib import Path

from pydantic import BaseModel

from app.core.files import ProjectFile
from app.core.frames import read_frame_file_for_display, write_frame_file
from app.core.ids import ID
from app.core.judgments import Judgment
from app.core.run_status import StageStatus
from app.core.text_sources import read_every_page_text
from app.models.locators import PageCharRange
from app.models.records.workflow_version import Method
from app.services.errors import WorkflowLoadError
from app.services.versioning import load_version
from app.services.workspace import resolve_project_dir
from app.services.review_packet.checksums import compute_sha256
from app.models.run_manifest import InputBinding
from app.services.review_packet.views import (
    PublishedSpan,
    RunArchive,
    RunView,
    StageJudgments,
    StageView,
    build_source_page_path,
)

DATA_DIR = "data"
RAW_DIR = "data/raw"
INPUTS_DIR = "inputs"
ARTIFACTS_DIR = "artifacts"
MANIFEST_FILE = "manifest.json"
EVENTS_FILE = "events.jsonl"
WORKFLOW_FILE = "workflow.json"
DOCUMENT_FILE = "methodology.md"
TERMS_FILE = "terms.json"
SOURCES_FILE = "sources.json"
JUDGMENTS_FILE = "judgments.jsonl"
SPANS_FILE = "spans.json"

REPORT_TYPE = "report"
# The two statuses whose handler ran to the end, so its files are on disk. The same
# pair app.web.run_header lists the run page's outputs from.
PUBLISHED_STATUSES = frozenset({StageStatus.OK, StageStatus.VALIDATION_WARNINGS})


# Reported on the index rather than dropped: a silent gap reads as an absence of
# data, which is a different and much worse claim than "this file was missing".
class OmittedFile(BaseModel):
    path: str
    reason: str


class DataReport(BaseModel):
    written: list[str]
    omitted: list[OmittedFile]
    # The published files, kept apart from the rest of `written` because they are the
    # run's RESULT — the index leads with them rather than listing them among the
    # records that explain how they were reached.
    artifacts: list[str]
    # (source id, page) of each page text written, so a page links only to those.
    cited_pages: set[tuple[str, int]]


class PacketSource(BaseModel):
    """One file the run read. `id` is its stored file's; None for one read from outside the store."""

    id: ID | None
    filename: str
    sha256: str | None
    origin_url: str | None
    # None for an upload, a file outside the store, or a stored file since deleted.
    fetched_at: str | None
    # Where this folder holds the bytes; None where they could not be copied.
    packet_path: str | None


def write_packet_data(
    root: Path,
    run_dir: Path,
    project_id: str,
    view: RunView,
    workflow: str | None,
    manifest: str,
    events: str,
    stage_sources: dict[str, Path | None],
    archive: RunArchive,
) -> DataReport:
    report = DataReport(written=[], omitted=[], artifacts=[], cited_pages=set())
    _write_run_records(root, manifest, events, report)
    _write_workflow(root, workflow, view, report)
    _write_pinned_method(root, project_id, view.workflow_version, report)
    _copy_published_artifacts(root, run_dir, view, report)
    for stage in view.stages:
        # Pre-resolved by the caller: joining a run dir to a recorded output_path is
        # app.runtime.manifest's alone, and this layer may not import it.
        _write_stage_output(root, stage, stage_sources.get(stage.stage_id), report)
    for sidecar in archive.lineage_sidecars.values():
        _copy_file(sidecar, root / RAW_DIR / sidecar.name, f"{RAW_DIR}/{sidecar.name}", report)
    copies = [
        _copy_input_file(root, binding, project_id, index, report)
        for index, binding in enumerate(view.inputs)
    ]
    sources = _write_sources(root, view.inputs, copies, report)
    _write_judgments(root, project_id, archive.judgments, view.is_test_run, report)
    _write_spans(root, archive, report)
    _write_cited_pages(root, archive.spans, sources, report)
    return report


def _write_run_records(root: Path, manifest: str, events: str, report: DataReport) -> None:
    _write_text(root / MANIFEST_FILE, manifest, MANIFEST_FILE, report)
    # The events carry the LLM prompts — the only record of what a model was asked.
    _write_text(root / EVENTS_FILE, events, EVENTS_FILE, report)


def _write_workflow(
    root: Path, workflow: str | None, view: RunView, report: DataReport
) -> None:
    if workflow is None:
        report.omitted.append(
            OmittedFile(
                path=WORKFLOW_FILE,
                reason=(
                    f"this run pinned workflow version {view.workflow_version!r}, "
                    "which could not be read"
                ),
            )
        )
        return
    _write_text(root / WORKFLOW_FILE, workflow, WORKFLOW_FILE, report)


def _write_pinned_method(
    root: Path, project_id: str, version_id: str | None, report: DataReport
) -> None:
    method = _read_pinned_method(project_id, version_id)
    if isinstance(method, str):
        report.omitted += [OmittedFile(path=path, reason=method)
                           for path in (DOCUMENT_FILE, TERMS_FILE)]
        return
    terms = method.model_dump_json(include={"row_types", "verbs"}, indent=2)
    _write_text(root / TERMS_FILE, terms, TERMS_FILE, report)
    if method.methodology is None:
        report.omitted.append(OmittedFile(
            path=DOCUMENT_FILE,
            reason=f"the project had no methodology when workflow version {version_id!r} was saved"))
        return
    _write_text(root / DOCUMENT_FILE, method.methodology, DOCUMENT_FILE, report)


def _read_pinned_method(project_id: str, version_id: str | None) -> Method | str:
    """A str says why the run holds no pinned method."""
    if version_id is None:
        return "this run records no workflow version"
    try:
        method = load_version(project_id, version_id).method
    except (FileNotFoundError, WorkflowLoadError):
        return f"this run pinned workflow version {version_id!r}, which could not be read"
    if method is None:
        return (
            f"this run's workflow version {version_id!r} predates versions keeping the "
            "methodology; the project's current text may differ, so it is not written here")
    return method


def _copy_published_artifacts(
    root: Path, run_dir: Path, view: RunView, report: DataReport
) -> None:
    source_root = run_dir / ARTIFACTS_DIR
    # Verbatim, at depth: a report function links across the layout it chose.
    files = sorted(
        p for p in source_root.rglob("*")
        if p.is_file() and not _is_hidden(p.relative_to(source_root))
    )
    for path in files:
        relative = f"{ARTIFACTS_DIR}/{path.relative_to(source_root).as_posix()}"
        written = _copy_file(path, root / relative, relative, report)
        if written is not None:
            report.artifacts.append(written)
    if not files:
        _report_unwritten_artifacts(view, report)


def _report_unwritten_artifacts(view: RunView, report: DataReport) -> None:
    published = [
        s.stage_id
        for s in view.stages
        if s.type == REPORT_TYPE and s.status in PUBLISHED_STATUSES
    ]
    if not published:
        return
    report.omitted.append(
        OmittedFile(
            path=f"{ARTIFACTS_DIR}/",
            reason=(
                f"{', '.join(published)} finished, but wrote no file to the run's "
                "artifacts folder — this run published nothing"
            ),
        )
    )


def _is_hidden(relative: Path) -> bool:
    """RELATIVE to the artifacts root — an absolute path under `.claude/` hides all."""
    return any(part.startswith(".") for part in relative.parts)  # .DS_Store is not published


def _write_stage_output(
    root: Path, stage: StageView, source: Path | None, report: DataReport
) -> None:
    # A CSV round trip loses dtypes, so the raw file is what a reviewer recomputes against.
    if source is None:
        report.omitted.append(
            OmittedFile(
                path=f"{DATA_DIR}/{stage.stage_id}.csv",
                reason=f"stage {stage.stage_id!r} finished {stage.status} and recorded no output",
            )
        )
        return
    if not source.is_file():
        report.omitted.append(
            OmittedFile(
                path=f"{DATA_DIR}/{stage.stage_id}.csv",
                reason=f"output file missing on disk: {stage.output_path}",
            )
        )
        return
    _write_csv(root, source, stage.stage_id, report)
    _copy_file(
        source,
        root / RAW_DIR / f"{stage.stage_id}{source.suffix}",
        f"{RAW_DIR}/{stage.stage_id}{source.suffix}",
        report,
    )


def _write_csv(root: Path, source: Path, stage_id: str, report: DataReport) -> None:
    relative = f"{DATA_DIR}/{stage_id}.csv"
    dest = root / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_frame_file(read_frame_file_for_display(source), dest)
    report.written.append(relative)


def _copy_input_file(
    root: Path, binding: InputBinding, project_id: str, index: int, report: DataReport
) -> str | None:
    recorded = Path(binding.path)
    relative = f"{INPUTS_DIR}/{index:02d}-{binding.stage_id}{recorded.suffix}"
    source = _locate_input(binding, project_id)
    if source is None:
        report.omitted.append(
            OmittedFile(
                path=relative,
                reason=(
                    f"input bound by stage {binding.stage_id!r} is no longer at "
                    f"{binding.path!r}"
                ),
            )
        )
        return None
    return _copy_file(source, root / relative, relative, report)


def _write_sources(
    root: Path, bindings: list[InputBinding], copies: list[str | None], report: DataReport
) -> list[PacketSource]:
    # Two stages reading one file list it once.
    listed: dict[str, PacketSource] = {}
    for binding, copy in zip(bindings, copies):
        listed.setdefault(binding.file_id or binding.path, _build_packet_source(binding, copy))
    sources = list(listed.values())
    if sources:
        _write_text(root / SOURCES_FILE, _dump_models(sources), SOURCES_FILE, report)
    return sources


def _build_packet_source(binding: InputBinding, copy: str | None) -> PacketSource:
    stored = None if binding.file_id is None else ProjectFile.load_or_none(binding.file_id)
    return PacketSource(
        id=binding.file_id, filename=binding.filename, sha256=binding.sha256,
        origin_url=binding.origin_url, fetched_at=None if stored is None else stored.fetched_at,
        packet_path=copy,
    )


def _write_judgments(
    root: Path, project_id: str, stages: list[StageJudgments], is_test_run: bool,
    report: DataReport,
) -> None:
    named = dict.fromkeys(judgment_id for stage in stages for judgment_id in stage.judgment_ids)
    stored = {judgment_id: _load_judgment(project_id, judgment_id) for judgment_id in named}
    for stage in stages:
        gaps = _list_judgment_gaps(stage, stored, is_test_run)
        if gaps:
            report.omitted.append(OmittedFile(
                path=JUDGMENTS_FILE, reason=f"stage {stage.stage_id!r}: {'; '.join(gaps)}"))
    held = [judgment for judgment in stored.values() if judgment is not None]
    if held:
        lines = "".join(f"{judgment.model_dump_json()}\n" for judgment in held)
        _write_text(root / JUDGMENTS_FILE, lines, JUDGMENTS_FILE, report)


def _load_judgment(project_id: str, judgment_id: ID) -> Judgment | None:
    judgment = Judgment.read_only().get(judgment_id)
    return judgment if judgment is not None and judgment.project_id == project_id else None


def _list_judgment_gaps(
    stage: StageJudgments, stored: dict[str, Judgment | None], is_test_run: bool
) -> list[str]:
    unstored = sum(1 for judgment_id in stage.judgment_ids if stored[judgment_id] is None)
    replayed, computed = stage.replayed_without_judgment, stage.computed_without_judgment
    gaps = []
    if unstored:
        gaps.append(f"{unstored} judgment(s) its rows name are not stored in this project")
    if replayed:
        gaps.append(f"{replayed} row(s) were replayed from cache entries recorded before "
                    "judgments were kept")
    if computed:
        gaps.append(f"{computed} row(s) were decided in a test run, which records no judgment"
                    if is_test_run else f"{computed} row(s) name no judgment in the run log")
    return gaps


def _write_spans(root: Path, archive: RunArchive, report: DataReport) -> None:
    if archive.published_stages_without_schema:
        stages = ", ".join(archive.published_stages_without_schema)
        report.omitted.append(OmittedFile(path=SPANS_FILE, reason=(
            f"the version this run pinned names no columns for {stages}, so which of "
            "their published cells hold spans is unknown")))
    if archive.spans:
        _write_text(root / SPANS_FILE, _dump_models(archive.spans), SPANS_FILE, report)


def _write_cited_pages(
    root: Path, spans: list[PublishedSpan], sources: list[PacketSource], report: DataReport
) -> None:
    copies = {source.id: source.packet_path for source in sources if source.id is not None}
    for source_id, pages in _group_verified_pages(spans).items():
        copy = copies.get(source_id)
        if copy is None:
            report.omitted += [
                OmittedFile(path=build_source_page_path(source_id, page),
                            reason=f"this folder holds no copy of source {source_id!r}")
                for page in pages
            ]
            continue
        _write_source_pages(root, root / copy, source_id, pages, report)


def _group_verified_pages(spans: list[PublishedSpan]) -> dict[str, list[int]]:
    pages: dict[str, set[int]] = {}
    for published in spans:
        locator = published.span.locator
        if published.refusal is None and isinstance(locator, PageCharRange):
            pages.setdefault(published.span.source_id, set()).add(locator.page)
    return {source_id: sorted(held) for source_id, held in sorted(pages.items())}


def _write_source_pages(
    root: Path, copy: Path, source_id: ID, pages: list[int], report: DataReport
) -> None:
    wanted = set(pages)
    for page, text in enumerate(islice(read_every_page_text(copy), pages[-1]), start=1):
        if page in wanted:
            relative = build_source_page_path(source_id, page)
            dest = root / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Untranslated newlines: a span's character offsets index this text.
            dest.write_text(text, encoding="utf-8", newline="")
            report.written.append(relative)
            report.cited_pages.add((source_id, page))


def _dump_models(models: Sequence[BaseModel]) -> str:
    return json.dumps([model.model_dump(mode="json") for model in models], indent=2) + "\n"


def _locate_input(binding: InputBinding, project_id: str) -> Path | None:
    """Where the run read it, or where the project moved it to — never a same-named guess."""
    recorded = Path(binding.path)
    if recorded.is_file():
        return recorded
    # Bindings hold absolute paths, so relocating a project staled every one of
    # them. The run recorded what it hashed, and only that hash can say a file
    # under the project's new root is the file the run actually read.
    parts = recorded.parts
    if not binding.sha256 or project_id not in parts:
        return None
    moved = resolve_project_dir(project_id).joinpath(*parts[parts.index(project_id) + 1:])
    if not moved.is_file() or compute_sha256(moved) != binding.sha256:
        return None
    return moved


def _copy_file(source: Path, dest: Path, relative: str, report: DataReport) -> str | None:
    """None means the copy did not happen and `report.omitted` says why."""
    if not source.is_file():
        report.omitted.append(
            OmittedFile(path=relative, reason=f"not found in the run directory: {source.name}")
        )
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    report.written.append(relative)
    return relative


def _write_text(dest: Path, text: str, relative: str, report: DataReport) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    report.written.append(relative)
