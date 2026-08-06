"""The runs index rows: when a run happened, what it ran, how it came out (the
same stage strip the run page draws), and what this run itself did differently.
The run id is no longer a column — it is the row's link target."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ValidationError

from app.models.run_manifest import RunManifest
from app.web.loading import load_manifest_model, runs_dir
from app.web.run_header import (
    VersionNote,
    describe_run_duration,
    read_file_name,
    read_version_note,
)
from app.web.stage_strip import StageStrip, build_stage_strip, describe_stage_tallies

_UNREADABLE_STATUS = "corrupt"


class RunIndexRow(BaseModel):
    """`strip` is None for a run whose manifest could not be parsed."""

    run_id: str
    status: str
    started_at: str | None = None
    duration: str | None = None
    version: VersionNote | None = None
    input_names: list[str] = []
    strip: StageStrip | None = None
    # The strip's counts in words, for the result cell's tooltip: the squares
    # carry the colour, this carries what the colours say.
    result_summary: str = ""
    differences: list[str] = []
    is_test_run: bool = False


def build_run_index_rows(project: str) -> list[RunIndexRow]:
    """One row per manifest-backed run of `project`, newest first."""
    # A manifest this reader cannot parse yields an identity-only row rather
    # than counts it never read, so one unreadable run never takes the index
    # down with it.
    directory = runs_dir(project)
    if not directory.is_dir():
        return []
    seen_versions: dict[str, VersionNote] = {}
    return [
        _build_row(project, run, seen_versions)
        for run in sorted(directory.iterdir(), reverse=True)
        if run.is_dir() and (run / "manifest.json").exists()
    ]


def describe_run_differences(manifest: RunManifest) -> list[str]:
    """What THIS run did differently — its own settings, not a diff against another run."""
    differences = [
        f"first {cap} rows of {stage_id}"
        for stage_id, cap in sorted(manifest.parameters.limits.items())
    ]
    if manifest.parameters.bust_cache:
        differences.append("cache off")
    if manifest.parameters.is_test_run:
        differences.append("test run")
    return differences


def read_input_file_names(manifest: RunManifest) -> list[str]:
    """The basename of each file this run read — never the absolute path."""
    names = []
    for binding in manifest.input_bindings.values():
        path = binding.get("path")
        if path:
            names.append(read_file_name(str(path)))
    return names


def _build_row(
    project: str, run: Path, seen_versions: dict[str, VersionNote]
) -> RunIndexRow:
    try:
        manifest = load_manifest_model(run)
    except ValidationError:  # also how the model reports unparseable JSON
        return RunIndexRow(run_id=run.name, status=_UNREADABLE_STATUS)
    persisted = manifest.to_dict()
    strip = build_stage_strip(persisted)
    return RunIndexRow(
        run_id=run.name,
        status=str(manifest.status),
        started_at=manifest.started_at,
        duration=describe_run_duration(persisted),
        version=_read_version(project, manifest.workflow_version, seen_versions),
        input_names=read_input_file_names(manifest),
        strip=strip,
        result_summary=describe_stage_tallies(strip),
        differences=describe_run_differences(manifest),
        is_test_run=manifest.parameters.is_test_run,
    )


def _read_version(
    project: str, version_id: str | None, seen: dict[str, VersionNote]
) -> VersionNote:
    """Resolved once per distinct version id, not once per run listed."""
    key = version_id or ""
    if key not in seen:
        seen[key] = read_version_note(project, version_id)
    return seen[key]
