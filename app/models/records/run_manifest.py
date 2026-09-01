from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ConfigDict, field_validator, model_validator

from app.core.json_types import JsonDict
from app.core.record import PersistedModel, PersistenceScope
from app.core.run_status import RunStatus
from app.models.run_manifest import StageRecord
from app.models.stage_contribution import QueueStats
from app.models.run_parameters import RunParameters


# docs/run-manifest.md
_LEGACY_PARAMETER_KEYS = {
    "limits": "limit_overrides",
    "offsets": "offset_overrides",
    "bust_cache": "bust_cache",
    "queue_auto_approve": "queue_auto_approve",
    "is_test_run": "is_test_run",
    "run_bindings": "run_bindings",
}


# docs/run-manifest.md
PRODUCTION_RUNS = "runs"


EVAL_RUNS = "eval_run"


RUN_AREAS = (PRODUCTION_RUNS, EVAL_RUNS)


_STORE_BOOKKEEPING = {"id", "created_at", "updated_at"}


class RunManifest(PersistedModel):
    """The run's living record, stored as `run/<project>/<run_id>`."""

    collection: ClassVar[str] = "run"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.RUN
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid",
                              validate_default=True, populate_by_name=True)
    # docs/run-manifest.md
    DUMP_OPTS: ClassVar[JsonDict] = {"exclude_unset": True}

    run_id: str
    started_at: str
    # Required: a run that cannot name its project has no id to be stored under.
    project: str
    workflow_version: str | None
    # What the caller asked of this run, verbatim — the settings a resume replays.
    parameters: RunParameters = RunParameters()
    # A RESULT, not a parameter: what the run found at prepare time.
    input_bindings: dict[str, dict[str, Any]] = {}
    # Required, no default: it would let a pre-rename manifest parse silently, hiding queued items.
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
        # The flat keys MOVE: this model forbids extras, so both spellings would not load.
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
        # Unmarked so `exclude_unset` writes no key. docs/run-manifest.md
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

    def to_dict_without_tracebacks(self) -> dict[str, Any]:
        """A traceback names host paths, and its reader quotes them onward."""
        return self.model_dump(
            exclude_unset=True,
            exclude={**dict.fromkeys(_STORE_BOOKKEEPING, True),
                     "stage_records": {"__all__": {"error": {"traceback"}}}},
        )
