"""The packet's data half: every stage's output, the run's own records, the
workflow it executed, and the input files it read."""
from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from app.core.frames import PARQUET_SUFFIX
from app.services.project import find_document_path
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
    root: Path, run_dir: Path, project_dir: Path, view: RunView, workflow: str | None
) -> DataReport:
    """Writes every non-HTML file of the packet under `root`."""
    # `workflow` is the pinned version as JSON, or None when it could not be read.
    report = DataReport(written=[], omitted=[])
    _copy_run_records(root, run_dir, report)
    _write_workflow(root, workflow, view, report)
    _copy_document(root, project_dir, report)
    for stage in view.stages:
        _write_stage_output(root, run_dir, stage, report)
    for index, binding in enumerate(view.inputs):
        _copy_input_file(root, binding.path, binding.stage_id, index, report)
    return report


def _copy_run_records(root: Path, run_dir: Path, report: DataReport) -> None:
    """The run's own two artifacts, verbatim."""
    # events.jsonl carries the LLM prompts — the only record of what a model was asked.
    _copy_file(run_dir / MANIFEST_FILE, root / MANIFEST_FILE, MANIFEST_FILE, report)
    _copy_file(run_dir / EVENTS_FILE, root / EVENTS_FILE, EVENTS_FILE, report)


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
    source = find_document_path(project_dir)
    if source is None:
        report.omitted.append(
            OmittedFile(path=DOCUMENT_FILE, reason="this project has no source document on disk")
        )
        return
    _copy_file(source, root / DOCUMENT_FILE, DOCUMENT_FILE, report)


def _write_stage_output(
    root: Path, run_dir: Path, stage: StageView, report: DataReport
) -> None:
    """CSV for reading, plus the raw file the run actually wrote."""
    # A CSV round trip loses dtypes, so the raw file is what a reviewer recomputes against.
    if stage.output_path is None:
        report.omitted.append(
            OmittedFile(
                path=f"{DATA_DIR}/{stage.stage_id}.csv",
                reason=f"stage {stage.stage_id!r} finished {stage.status} and recorded no output",
            )
        )
        return
    source = run_dir / stage.output_path
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
    read_stage_output(source).to_csv(dest, index=False)
    report.written.append(relative)


def read_stage_output(path: Path) -> pd.DataFrame:
    """A stage output file as a frame; the runtime writes parquet, or CSV when a
    frame would not serialize."""
    if path.suffix == PARQUET_SUFFIX:
        return pd.read_parquet(path)
    return pd.read_csv(path)


def _copy_input_file(
    root: Path, path: str, stage_id: str, index: int, report: DataReport
) -> None:
    """Named by stage, not by the author's filename."""
    # Two stages may bind files of the same name; the manifest records the real path.
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


__all__ = [
    "DATA_DIR",
    "DOCUMENT_FILE",
    "EVENTS_FILE",
    "INPUTS_DIR",
    "MANIFEST_FILE",
    "RAW_DIR",
    "WORKFLOW_FILE",
    "DataReport",
    "OmittedFile",
    "read_stage_output",
    "write_packet_data",
]
