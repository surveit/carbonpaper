"""The run manifest: its shape, minting one, storing it, and reading back the
frames its stages wrote. The executor (`app.runtime.executor`) is its single
writer. The per-stage pieces it embeds are `app.models.run_manifest`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd

from dataclasses import dataclass

from pydantic import ConfigDict, ValidationError, field_validator, model_validator

from app.core.errors import (
    DocumentNotFound,
    RunNotFoundError,
    StageNotInRun,
    StageOutputMissing,
)
from app.core.frames import read_frame_file
from app.core.persistence import (
    JsonDict,
    PersistedModel,
    PersistenceScope,
    get_store,
)
from app.core.run_status import RunStatus, StageStatus
from app.models import WorkflowStage
from app.models.run_manifest import QueueStats, StageRecord
from app.models.run_parameters import RunParameters

from .context import RunContext


# ─── The stored run manifest ─────────────────────────────────────────────────

# RunParameters field -> the top-level key a manifest written before the nesting
# carried it under. Read by `_lift_legacy_parameters`; may only grow.
_LEGACY_PARAMETER_KEYS = {
    "limits": "limit_overrides",
    "offsets": "offset_overrides",
    "bust_cache": "bust_cache",
    "queue_auto_approve": "queue_auto_approve",
    "is_test_run": "is_test_run",
    "run_bindings": "run_bindings",
}



# The run directory a production run lives under, and the `area` segment of its
# store key. `runs/` vs `eval_run/` was the discriminator before the manifest
# moved here, so keeping it means a project's production runs stay one prefix
# scan and an eval run can never appear in the runs index.
PRODUCTION_RUNS = "runs"

# PersistedModel's own fields. A run recorded none of them, so `to_dict` leaves
# them out of what every reader above this module consumes.
_STORE_BOOKKEEPING = {"id", "created_at", "updated_at"}


class RunManifest(PersistedModel):
    """The run's living record, stored as `run/<project>/<run_id>`."""

    collection: ClassVar[str] = "run"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.RUN
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid",
                              validate_default=True, populate_by_name=True)
    # The stored payload is the `exclude_unset` shape `to_dict` produces, so a
    # field the run never set is absent from the record too — `clear_halt`
    # depends on it: a stored `halted_at: null` would come back marked set and
    # re-appear in `to_dict` on the next read.
    DUMP_OPTS: ClassVar[JsonDict] = {"exclude_unset": True}

    run_id: str
    started_at: str
    # Required, unlike the `project` a subset run used to default to None: a run
    # that cannot name its project has no id to be stored under, and every caller
    # names one.
    project: str
    workflow_version: str | None
    # What the caller asked of this run, verbatim — the settings a resume replays.
    # `_lift_legacy_parameters` reads the flat pre-nesting keys off an older
    # manifest into it, so every run on disk still parses.
    parameters: RunParameters = RunParameters()
    # The preflight provenance of each bound input (its absolute path, a sha256 and
    # a byte count streamed at prepare time). A RESULT, not a parameter: it records
    # what the run found, not what it was asked for.
    input_bindings: dict[str, dict[str, Any]] = {}
    # The live human_review_queue tallies. Required, unlike the override maps
    # above: the key was renamed out of an older on-disk vocabulary, so a default
    # would let a pre-rename manifest parse and then report an empty tally for a
    # run that actually queued items. `create_run_manifest` always sets it.
    human_review_queue_stats: dict[str, QueueStats]
    dropped_columns: dict[str, list[str]] = {}
    status: RunStatus
    stage_records: list[StageRecord]
    finished_at: str | None = None
    halted_at: list[str] | None = None
    cancelled_at: str | None = None
    resumed_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy_parameters(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # The flat keys MOVE — never copied, never left behind. This model forbids
        # extras, so a payload carrying both spellings would not load at all.
        lifted = {k: v for k, v in data.items() if k not in _LEGACY_PARAMETER_KEYS.values()}
        if "parameters" in data:
            return lifted
        legacy = {new: data[old] for new, old in _LEGACY_PARAMETER_KEYS.items() if old in data}
        return {**lifted, "parameters": legacy} if legacy else lifted

    @model_validator(mode="after")
    def _always_write_the_store_bookkeeping(self) -> RunManifest:
        """`exclude_unset` must never drop the store's own fields."""
        self.__pydantic_fields_set__.update(_STORE_BOOKKEEPING)
        return self

    @field_validator("halted_at", mode="before")
    @classmethod
    def _halted_at_to_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [value]
        return value

    def settle_stage_records(self, records: list[StageRecord]) -> None:
        self.stage_records = records

    def record_dropped_columns(self, stage_id: str, columns: list[str]) -> None:
        self.dropped_columns[stage_id] = columns
        # Marked set so `exclude_unset` still emits it on a legacy manifest that lacked the key.
        self.__pydantic_fields_set__.add("dropped_columns")

    def record_human_review_queue_stats(self, stage_id: str, stats: QueueStats) -> None:
        self.human_review_queue_stats[stage_id] = stats

    def clear_halt(self) -> None:
        self.halted_at = None
        # Unmarked so `exclude_unset` writes no key: a resumed or cancelled run must not
        # show a review banner for a halt that no longer holds.
        self.__pydantic_fields_set__.discard("halted_at")

    def find_stage_record(self, stage_id: str) -> StageRecord | None:
        return next((r for r in self.stage_records if r.stage_id == stage_id), None)

    @staticmethod
    def compose_id(project_id: str, run_id: str, area: str = PRODUCTION_RUNS) -> str:
        """The store key; `area` is the directory that held the run."""
        return f"{project_id}/{area}/{run_id}"

    def to_dict(self) -> dict[str, Any]:
        """What this run RECORDED — the boundary shape every reader consumes."""
        return self.model_dump(exclude_unset=True, exclude=_STORE_BOOKKEEPING)




def create_run_manifest(
    ordered: list[WorkflowStage],
    ctx: RunContext,
    *,
    run_id: str,
    project_id: str,
    workflow_version: str | None,
    input_bindings: dict[str, dict[str, Any]],
    area: str = PRODUCTION_RUNS,
) -> RunManifest:
    return RunManifest(
        id=RunManifest.compose_id(project_id, run_id, area),
        run_id=run_id,
        started_at=datetime.now().isoformat(timespec="seconds"),
        project=project_id,
        workflow_version=workflow_version,
        parameters=ctx.params,
        input_bindings=input_bindings,
        human_review_queue_stats={},
        dropped_columns={},
        status=RunStatus.RUNNING,
        stage_records=[
            StageRecord.record_with_status(s.stage, StageStatus.PENDING)
            for s in ordered
        ],
    )


def write_manifest(manifest: RunManifest) -> None:
    """The single writer of a run record."""
    manifest.save()


def read_run_manifest(project_id: str, run_id: str, area: str = PRODUCTION_RUNS) -> RunManifest:
    """Raises RunNotFoundError when unrecorded, ValidationError on a bad payload."""
    try:
        return RunManifest.load(RunManifest.compose_id(project_id, run_id, area))
    except DocumentNotFound as exc:
        raise RunNotFoundError(f"no run '{run_id}' in project '{project_id}'") from exc


@dataclass
class RunEntry:
    """One recorded run at BOTH levels; callers disagree on what unreadable means."""

    run_id: str
    # `raw` is the stored payload, None when it is not even JSON. `manifest` is
    # that payload typed, None when this model rejects it (a run written before a
    # field was renamed). A caller needing ONE fact takes it off `raw`; one
    # needing the whole model waits for `manifest`.
    raw: JsonDict | None = None
    manifest: RunManifest | None = None


def list_run_entries(project_id: str) -> list[RunEntry]:
    """This project's PRODUCTION runs, oldest-first by id (a strftime stamp)."""
    prefix = f"{project_id}/{PRODUCTION_RUNS}/"
    # Ids first, then each payload on its own: one unreadable record must not take
    # down the listing of every other run.
    entries = [
        _read_entry(doc_id, doc_id[len(prefix):])
        for doc_id in get_store().list_ids(RunManifest.collection, prefix)
    ]
    return sorted(entries, key=lambda e: e.run_id)


@dataclass
class StoredRun:
    """A run entry carrying the two id segments `list_run_entries` already knew from its caller."""

    project: str
    # The directory the run was written under — `PRODUCTION_RUNS` for a production
    # run, `eval_run` for an eval's subset run. Both are stored in one collection.
    area: str
    entry: RunEntry


def list_stored_runs() -> list[StoredRun]:
    """Every run in the workspace, both areas and all projects — the whole-store scan."""
    return [
        _read_stored_run(doc_id)
        for doc_id in get_store().list_ids(RunManifest.collection)
    ]


def _read_stored_run(doc_id: str) -> StoredRun:
    project, area, run_id = _split_run_id(doc_id)
    return StoredRun(project=project, area=area, entry=_read_entry(doc_id, run_id))


def _split_run_id(doc_id: str) -> tuple[str, str, str]:
    """`compose_id` is the only writer of these keys, so a key of another shape is a bug."""
    segments = doc_id.split("/")
    if len(segments) != 3:
        raise ValueError(
            f"run record '{doc_id}' is not the project/area/run_id key compose_id writes"
        )
    return segments[0], segments[1], segments[2]


def _read_entry(doc_id: str, run_id: str) -> RunEntry:
    raw = get_store().read_tolerant(RunManifest.collection, doc_id)
    if raw is None:
        return RunEntry(run_id=run_id)
    try:
        return RunEntry(run_id=run_id, raw=raw, manifest=RunManifest.model_validate(raw))
    except ValidationError:
        return RunEntry(run_id=run_id, raw=raw)


def resolve_output_path(run_dir: Path, output_path: str | None) -> Path | None:
    if not output_path:
        return None
    resolved = (run_dir / output_path).resolve()
    if not resolved.is_relative_to(run_dir.resolve()):
        raise StageOutputMissing(
            f"recorded output path '{output_path}' escapes run '{run_dir.name}'"
        )
    return resolved


def read_stage_output_frame(project_id: str, run_dir: Path, stage_id: str) -> pd.DataFrame:
    records = read_run_manifest(project_id, run_dir.name).stage_records
    record = _find_stage_record(records, run_dir, stage_id)
    path = resolve_output_path(run_dir, record.output_path)
    if path is None:
        raise StageOutputMissing(
            f"stage '{stage_id}' of run '{run_dir.name}' wrote no output "
            f"(its status is '{record.status}'), so it holds no values to read"
        )
    return read_frame_file(path)


def _find_stage_record(
    records: list[StageRecord], run_dir: Path, stage_id: str
) -> StageRecord:
    for record in records:
        if record.stage_id == stage_id:
            return record
    ran = ", ".join(record.stage_id for record in records) or "(none)"
    raise StageNotInRun(
        f"run '{run_dir.name}' has no stage '{stage_id}' — the stages it ran: {ran}"
    )
