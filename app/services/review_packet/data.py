"""The packet's data half: every stage's output, the run's own records, the
workflow it executed, and the input files it read."""
from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel

from app.core.frames import read_frame_file, write_frame_file
from app.core.run_status import StageStatus
from app.services.methodology import read_methodology
from app.services.workspace import resolve_project_dir
from app.services.review_packet.checksums import compute_sha256
from app.models.run_manifest import InputBinding
from app.services.review_packet.views import RunView, StageView

DATA_DIR = "data"
RAW_DIR = "data/raw"
INPUTS_DIR = "inputs"
ARTIFACTS_DIR = "artifacts"
MANIFEST_FILE = "manifest.json"
EVENTS_FILE = "events.jsonl"
WORKFLOW_FILE = "workflow.json"
DOCUMENT_FILE = "methodology.md"

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


def write_packet_data(
    root: Path,
    run_dir: Path,
    project_id: str,
    view: RunView,
    workflow: str | None,
    manifest: str,
    events: str,
    stage_sources: dict[str, Path | None],
) -> DataReport:
    report = DataReport(written=[], omitted=[], artifacts=[])
    _write_run_records(root, manifest, events, report)
    _write_workflow(root, workflow, view, report)
    _write_document(root, project_id, report)
    _copy_published_artifacts(root, run_dir, view, report)
    for stage in view.stages:
        # Pre-resolved by the caller: joining a run dir to a recorded output_path is
        # app.runtime.manifest's alone, and this layer may not import it.
        _write_stage_output(root, stage, stage_sources.get(stage.stage_id), report)
    for index, binding in enumerate(view.inputs):
        _copy_input_file(root, binding, project_id, index, report)
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


def _write_document(root: Path, project_id: str, report: DataReport) -> None:
    document = read_methodology(project_id)
    if document is None:
        report.omitted.append(
            OmittedFile(path=DOCUMENT_FILE, reason="this project has no source document")
        )
        return
    _write_text(root / DOCUMENT_FILE, document, DOCUMENT_FILE, report)


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
    write_frame_file(read_frame_file(source), dest)
    report.written.append(relative)


def _copy_input_file(
    root: Path, binding: InputBinding, project_id: str, index: int, report: DataReport
) -> None:
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
        return
    _copy_file(source, root / relative, relative, report)


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
