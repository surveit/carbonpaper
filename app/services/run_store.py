"""run_store.py — the designated persistence for a run's own products.

Before this seam the runner wrote ``runs/<id>/manifest.json`` and its stage
outputs / review-queue snapshots ad-hoc, and the web layer read them back by
constructing those paths itself. Everything a run produces now flows through
one service:

  - the run **manifest** is a RUN-scoped document — ``RunManifest`` is the
    first ``PersistedModel`` carrying ``SCOPE = PersistenceScope.RUN`` (see
    ``app.core.persistence.PersistenceScope``): a record produced by one run
    and meaningless outside it. It lives in the document store keyed by
    ``<project>/<run_id>``, not as a JSON file a caller has to find;
  - a run's **stage outputs** and **review-queue snapshots** are frames —
    persisted through ``FrameStore`` (``app.core.frames``) rooted at the run's
    own directory, so the tabular bytes go through the same validated seam as
    every other frame instead of an ad-hoc ``to_parquet`` call.

The web layer reads runs through the functions here (``load_manifest``,
``list_run_manifests``, ``read_output_frame``, ``load_queue_snapshot``,
``load_queue_fingerprints``) rather than building paths under ``runs/<id>/``.
A non-production run (an eval subset, a preview) carries no ``project`` and so
mints no manifest document — ``persist_manifest`` is a no-op for it; its
frames are still written to its scratch run directory for the caller to read
back in memory.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd
import pyarrow.lib as pa_lib
from pydantic import BaseModel, ConfigDict, Field

from app.core.frames import PARQUET_SUFFIX, FrameStore
from app.core.persistence import JsonDict, PersistedModel, PersistenceScope
from app.core.run_status import RunStatus, StageStatus
from app.models import StageType

# The two on-disk sub-directories a run's frames live in, under its own run
# directory: computed stage outputs and pending review-queue snapshots.
_OUTPUTS = "outputs"
_QUEUE = "queue"


# ── The typed manifest shape ──────────────────────────────────────────────────


class _Strict(BaseModel):
    """Strict config mirroring PersistedModel's own (app.core.persistence), so
    an embedded manifest record validates and serializes under the same rules
    as the document that carries it — enums render as bare values, unknown
    keys are rejected."""

    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
        populate_by_name=True,
    )


class RunStageError(_Strict):
    """One failed stage's ``error`` record: the exception type name, a
    human-readable message, and its traceback (``None`` for a row-generation
    error, which has no single exception to format)."""

    type: str
    message: str
    traceback: str | None = None


class RunQueueStats(_Strict):
    """One human_review_queue stage's item counts for one run — the shape the
    handler records on ``RunContext.queue_stats`` and the manifest carries back."""

    items_queued_total: int
    items_passed_through: int
    items_pending: int
    items_decided: int


class RunQueueFingerprints(_Strict):
    """The fingerprints a halted queue stage's snapshot carries off to the
    side, never as snapshot columns: ``stage_fingerprint`` (shared by every
    pending row of that halt) and ``input_fingerprints`` (one per row,
    positionally aligned to the snapshot's row order)."""

    stage_fingerprint: str
    input_fingerprints: list[str]


class RunStageRecord(_Strict):
    """One stage's manifest record. Mirrors the dict the execution engine
    builds mid-run (``app.runtime.executor``): most fields are only populated
    once the stage reaches the matching point in its lifecycle, so all but
    ``stage_id`` carry a default. ``input_validation`` / ``output_validation``
    hold ``ValidationReport.to_dict()`` output, whose own fields are untyped —
    the sanctioned ``JsonDict`` boundary."""

    stage_id: str
    type: StageType | None = None
    name: str | None = None
    status: StageStatus = StageStatus.PENDING
    input_validation: list[JsonDict] = Field(default_factory=list)
    output_validation: JsonDict | None = None
    elapsed_ms: int = 0
    rows: int = 0
    error: RunStageError | None = None
    started_at: str | None = None
    finished_at: str | None = None
    output_path: str | None = None
    queue_path: str | None = None
    notes: list[str] = Field(default_factory=list)
    llm_usage: JsonDict | None = None


class RunManifest(PersistedModel):
    """A production run's manifest — the first RUN-scoped document.

    ``id`` is ``<project>/<run_id>`` (see ``manifest_id``). The manifest is
    produced by exactly one run and is meaningless outside it, which is what
    ``SCOPE = PersistenceScope.RUN`` declares. ``run_bindings`` /
    ``input_bindings`` are the sanctioned dynamic boundary
    (``app.core.persistence.JsonDict``): connector-param and preflight-provenance
    records the runner carries verbatim but does not otherwise model."""

    collection: ClassVar[str] = "run_manifest"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.RUN

    run_id: str
    project: str
    status: RunStatus = RunStatus.RUNNING
    stages: list[RunStageRecord] = Field(default_factory=list)
    workflow_version: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    limit_overrides: dict[str, int] = Field(default_factory=dict)
    offset_overrides: dict[str, int] = Field(default_factory=dict)
    run_bindings: JsonDict = Field(default_factory=dict)
    input_bindings: JsonDict = Field(default_factory=dict)
    queue_stats: dict[str, RunQueueStats] = Field(default_factory=dict)
    dropped_columns: dict[str, list[str]] = Field(default_factory=dict)
    halted_at: list[str] | None = None
    cancelled_at: str | None = None
    resumed_at: str | None = None


def manifest_id(project: str, run_id: str) -> str:
    """The store id for one run's manifest document: ``<project>/<run_id>``."""
    return f"{project}/{run_id}"


# ── Manifest persistence + reads ──────────────────────────────────────────────


def persist_manifest(manifest: JsonDict) -> None:
    """Persist a production run's manifest as its ``RunManifest`` document.

    The execution engine (``app.runtime.executor``) keeps its live manifest as
    a plain dict it mutates in place (its statuses are enum members); this is
    the single boundary where that dict becomes a stored document. A
    non-production run (a subset/eval or preview) carries no ``project``, so it
    mints no document — its manifest is scratch that the caller reads in memory.

    The dict is first normalized through ``json.dumps(default=str)`` — the same
    normalization the old on-disk manifest used — so any non-JSON-native cell
    (a numpy scalar, a Timestamp) becomes a string before validation, and the
    caller's in-memory manifest (which keeps its enum members) is never
    mutated."""
    project = manifest.get("project")
    if not project:
        return
    run_id = manifest["run_id"]
    safe: JsonDict = json.loads(json.dumps(manifest, default=str))
    RunManifest.model_validate({**safe, "id": manifest_id(project, run_id)}).save()


def load_manifest(project: str, run_id: str) -> JsonDict | None:
    """One run's manifest as a plain dict (bare-string statuses), or ``None`` if
    no such run. ``exclude_none`` drops unset optional fields so the dict shape
    mirrors the record the engine builds — a field only appears once it has a
    value (e.g. ``halted_at`` / ``queue_path`` are absent, not null, until they
    do) — and every existing manifest reader keeps working unchanged; only the
    source moved from a JSON file to the document store."""
    doc = RunManifest.load_or_none(manifest_id(project, run_id))
    return doc.model_dump(mode="json", exclude_none=True) if doc is not None else None


def manifest_exists(project: str, run_id: str) -> bool:
    return RunManifest.exists(manifest_id(project, run_id))


def list_run_manifests(project: str) -> list[JsonDict]:
    """Every run of ``project``, newest first (run ids are strftime timestamps,
    so a reverse lexical sort is chronological), each dumped to the same dict
    shape ``load_manifest`` returns."""
    docs = RunManifest.list(prefix=f"{project}/")
    docs.sort(key=lambda d: d.run_id, reverse=True)
    return [d.model_dump(mode="json", exclude_none=True) for d in docs]


def count_runs(project: str) -> int:
    """Number of runs recorded for ``project`` — a run counts iff it has a
    manifest document (the store equivalent of the old 'has a manifest.json')."""
    return len(RunManifest.list(prefix=f"{project}/"))


# ── Frame persistence + reads (rooted at the run's own directory) ─────────────


@dataclass(frozen=True)
class SavedFrame:
    """The result of persisting a run frame: its path relative to the run
    directory (POSIX, so the stored manifest is identical on every platform),
    and a note when a parquet-incompatible frame fell back to CSV — ``None``
    on the normal parquet path."""

    rel_path: str
    fallback_note: str | None


def _save_frame_with_csv_fallback(
    run_dir: Path, collection: str, stage_id: str, frame: pd.DataFrame
) -> SavedFrame:
    """Persist ``frame`` through ``FrameStore`` rooted at ``run_dir`` as
    ``<collection>/<stage_id>.parquet``, falling back to CSV for a column whose
    dtype/shape parquet can't represent (mixed-type object columns, nested
    Python values). A disk/OS error is NOT caught — it would fail identically
    for CSV, so it propagates rather than silently degrading the frame."""
    try:
        FrameStore(run_dir).save_frame(collection, stage_id, frame)
        return SavedFrame(f"{collection}/{stage_id}{PARQUET_SUFFIX}", None)
    except (pa_lib.ArrowException, ValueError, TypeError) as exc:
        csv_path = run_dir / collection / f"{stage_id}.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(csv_path, index=False)
        return SavedFrame(f"{collection}/{stage_id}.csv", f"Wrote CSV instead of parquet: {exc}")


def save_output_frame(run_dir: Path, stage_id: str, frame: pd.DataFrame) -> SavedFrame:
    """Persist a stage's output frame under the run's ``outputs/`` directory."""
    return _save_frame_with_csv_fallback(run_dir, _OUTPUTS, stage_id, frame)


def read_output_frame(run_dir: Path, rel_path: str) -> pd.DataFrame:
    """Read a run frame (parquet or CSV, by suffix) at ``rel_path`` relative to
    ``run_dir``. Raises ``FileNotFoundError`` if it is not on disk."""
    path = run_dir / rel_path
    if not path.exists():
        raise FileNotFoundError(f"run frame missing on disk: {rel_path}")
    return _read_frame_file(path)


def read_output_frame_or_none(run_dir: Path, rel_path: str) -> pd.DataFrame | None:
    """A run frame at ``rel_path``, or ``None`` if it is missing, corrupt, or
    otherwise unreadable — the tolerant read a resume uses, where a prior
    output that can't be reloaded is simply treated as not-yet-produced so the
    stage re-runs, rather than failing the resume."""
    path = run_dir / rel_path
    if not path.exists():
        return None
    try:
        return _read_frame_file(path)
    except (pa_lib.ArrowException, pd.errors.ParserError, OSError, ValueError):
        return None


def _read_frame_file(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix == PARQUET_SUFFIX else pd.read_csv(path)


def save_queue_snapshot(run_dir: Path, stage_id: str, pending: pd.DataFrame) -> Path:
    """Persist a halted queue stage's PURE pending-rows snapshot under the run's
    ``queue/`` directory and return its absolute path (the reviewer UI reads it,
    and the runner records it as the stage's ``queue_path``)."""
    saved = _save_frame_with_csv_fallback(run_dir, _QUEUE, stage_id, pending)
    return run_dir / saved.rel_path


def load_queue_snapshot(run_dir: Path, stage_id: str) -> pd.DataFrame | None:
    """A halted queue stage's snapshot frame (parquet or CSV), or ``None`` if no
    run has halted at this stage."""
    for ext in (PARQUET_SUFFIX, ".csv"):
        path = run_dir / _QUEUE / f"{stage_id}{ext}"
        if path.exists():
            return _read_frame_file(path)
    return None


def _fingerprints_path(run_dir: Path, stage_id: str) -> Path:
    return run_dir / _QUEUE / f"{stage_id}.fingerprints.json"


def save_queue_fingerprints(
    run_dir: Path, stage_id: str, stage_fingerprint: str, input_fingerprints: list[str]
) -> None:
    """Persist the fingerprints a halted queue snapshot carries off to the side
    (never as snapshot columns): the one ``stage_fingerprint`` every pending row
    shares and the per-row ``input_fingerprints``, positionally aligned to the
    snapshot's row order."""
    path = _fingerprints_path(run_dir, stage_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"stage_fingerprint": stage_fingerprint, "input_fingerprints": input_fingerprints}
        ),
        encoding="utf-8",
    )


def load_queue_fingerprints(run_dir: Path, stage_id: str) -> RunQueueFingerprints | None:
    """The fingerprints sidecar a halted human_review_queue stage wrote beside
    its snapshot. ``None`` if no run has halted at this stage yet.

    Raises ``ValueError`` if the snapshot exists but its row count doesn't match
    ``input_fingerprints``' length: positional alignment between the two is not
    something to guess at silently when it can't be verified."""
    path = _fingerprints_path(run_dir, stage_id)
    if not path.exists():
        return None
    data: JsonDict = json.loads(path.read_text(encoding="utf-8"))
    fingerprints = RunQueueFingerprints(
        stage_fingerprint=data["stage_fingerprint"],
        input_fingerprints=data["input_fingerprints"],
    )
    snapshot = load_queue_snapshot(run_dir, stage_id)
    if snapshot is not None and len(snapshot) != len(fingerprints.input_fingerprints):
        raise ValueError(
            f"queue fingerprints sidecar for stage '{stage_id}' in run "
            f"'{run_dir.name}' names {len(fingerprints.input_fingerprints)} row(s) "
            f"but the snapshot has {len(snapshot)} — alignment cannot be trusted"
        )
    return fingerprints


__all__ = [
    "RunManifest",
    "RunStageRecord",
    "RunStageError",
    "RunQueueStats",
    "RunQueueFingerprints",
    "SavedFrame",
    "manifest_id",
    "persist_manifest",
    "load_manifest",
    "manifest_exists",
    "list_run_manifests",
    "count_runs",
    "save_output_frame",
    "read_output_frame",
    "read_output_frame_or_none",
    "save_queue_snapshot",
    "load_queue_snapshot",
    "save_queue_fingerprints",
    "load_queue_fingerprints",
]
