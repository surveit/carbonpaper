"""The packet's data half: every stage's output, the run's own records, the
workflow it executed, and the input files it read."""
from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel

from app.core.frames import read_frame_file, write_frame_file
from app.services.methodology import read_methodology
from app.services.review_packet.views import RunView, StageView

DATA_DIR = "data"
RAW_DIR = "data/raw"
INPUTS_DIR = "inputs"
MANIFEST_FILE = "manifest.json"
EVENTS_FILE = "events.jsonl"
WORKFLOW_FILE = "workflow.json"
DOCUMENT_FILE = "methodology.md"


# Reported on the index rather than dropped: a silent gap reads as an absence of
# data, which is a different and much worse claim than "this file was missing".
class OmittedFile(BaseModel):
    """A file the packet expected and could not write."""

    path: str
    reason: str


class DataReport(BaseModel):
    written: list[str]
    omitted: list[OmittedFile]


def write_packet_data(
    root: Path,
    run_dir: Path,
    project_dir: Path,
    view: RunView,
    workflow: str | None,
    manifest: str,
    events: str,
    stage_sources: dict[str, Path | None],
) -> DataReport:
    report = DataReport(written=[], omitted=[])
    # `workflow` is the pinned version as JSON, or None when it could not be read.
    # `manifest`/`events` are this run's records, already serialized by the caller.
    _write_run_records(root, manifest, events, report)
    _write_workflow(root, workflow, view, report)
    _copy_document(root, project_dir, report)
    for stage in view.stages:
        # Pre-resolved by the caller: joining a run dir to a recorded output_path is
        # app.runtime.manifest's alone, and this layer may not import it.
        _write_stage_output(root, stage, stage_sources.get(stage.stage_id), report)
    for index, binding in enumerate(view.inputs):
        _copy_input_file(root, binding.path, binding.stage_id, index, report)
    return report


def _write_run_records(root: Path, manifest: str, events: str, report: DataReport) -> None:
    # The events carry the LLM prompts — the only record of what a model was asked.
    _write_text(root / MANIFEST_FILE, manifest, MANIFEST_FILE, report)
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


def _copy_document(root: Path, project_dir: Path, report: DataReport) -> None:
    """The authored methodology the workflow was compiled from."""
    document = read_methodology(project_dir.name)
    if document is None:
        report.omitted.append(
            OmittedFile(path=DOCUMENT_FILE, reason="this project has no source document")
        )
        return
    _write_text(root / DOCUMENT_FILE, document, DOCUMENT_FILE, report)


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
    root: Path, path: str, stage_id: str, index: int, report: DataReport
) -> None:
    # Named by stage: two stages may bind files of the same name.
    source = Path(path)
    relative = f"{INPUTS_DIR}/{index:02d}-{stage_id}{source.suffix}"
    if not path or not source.is_file():
        report.omitted.append(
            OmittedFile(
                path=relative,
                reason=f"input bound by stage {stage_id!r} is no longer at {path!r}",
            )
        )
        return
    _copy_file(source, root / relative, relative, report)


def _copy_file(source: Path, dest: Path, relative: str, report: DataReport) -> None:
    if not source.is_file():
        report.omitted.append(
            OmittedFile(path=relative, reason=f"not found in the run directory: {source.name}")
        )
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    report.written.append(relative)


def _write_text(dest: Path, text: str, relative: str, report: DataReport) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    report.written.append(relative)
