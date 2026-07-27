"""An un-stubbed `call_llm` raises `LLMError` rather than reaching the real `claude`
CLI; a test exercising the LLM boundary must monkeypatch it itself. The per-test
document and frame stores are configured ahead of the app's startup wiring, which
leaves an already-configured store alone."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.runtime.context import (
    RunContext,
    RunIdentity,
    RunMode,
)
from app.runtime.manifest import CONTRIBUTION_ATTR, StageContribution
from app.core.stage_cache import ReadOnlyStageCache


def contribution_of(frame: pd.DataFrame) -> StageContribution:
    """The StageContribution a handler attached to its output frame's `.attrs`.
    A handler reports its usage/errors/dropped-columns/queue tallies here (the
    executor merges it into the manifest), so a direct-handler test reads them
    off the returned frame rather than off the context."""
    return frame.attrs[CONTRIBUTION_ATTR]


@pytest.fixture(autouse=True)
def offline_llm(monkeypatch):
    monkeypatch.setattr("app.runtime.options.agent_available", lambda: False)


@pytest.fixture(autouse=True)
def fresh_store():
    from app.core.persistence import SqliteKvStore, configure_store
    configure_store(SqliteKvStore(":memory:"))


@pytest.fixture(autouse=True)
def fresh_frame_store(tmp_path):
    from app.core.frames import FrameStore, configure_frame_store
    configure_frame_store(FrameStore(tmp_path / "frames"))


@pytest.fixture(autouse=True)
def reset_cancellation_registry():
    """The cancel registry is process-global and production never removes keys
    (see app.runtime.cancellation), so reset it around each test to keep runs
    independent."""
    from app.runtime.cancellation import reset
    reset()
    yield
    reset()


def make_run_context(
    *,
    repo_root: Path = Path("."),
    run_dir: Path = Path("."),
    identity: RunIdentity | None = None,
    stage_cache: ReadOnlyStageCache | None = None,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bust_cache: bool = False,
) -> RunContext:
    """A RunContext for tests that only care about a few of its fields. `mode`
    follows project scope: an `identity` (with its `stage_cache`) makes it a
    production run, otherwise a non-production run. A stage's telemetry is reported on
    its output frame's `.attrs`, not on the context, so there is nothing to seed
    here."""
    mode: RunMode = "production" if identity is not None else "non_production"
    return RunContext(
        mode=mode, repo_root=repo_root, run_dir=run_dir,
        identity=identity, stage_cache=stage_cache,
        limits=dict(limits or {}), offsets=dict(offsets or {}),
        bust_cache=bust_cache,
    )


def queue_columns(source: str = "score", target: str = "human_score") -> dict[str, object]:
    """The queue-column names a human_review_queue fixture declares when the
    test is about something else (halting, caching, counts). `source` is the
    input column reviewed, which must exist on the frame the fixture runs the
    stage over — the runtime raises if it does not."""
    return {
        "reviewed_columns": {source: target},
        "verdict_column": "decision",
        "reviewer_column": "reviewer_id",
        "reviewed_at_column": "reviewed_at",
        "review_notes_column": "review_notes",
    }


QUEUE_COLUMNS: dict[str, object] = queue_columns()


def queue_added_columns(
    target: str = "human_score", target_type: str = "int"
) -> list[dict[str, object]]:
    """The output_schema columns a `queue_columns()` stage adds, to append to
    whatever the fixture's input schema declares. Every one but the verdict is
    nullable: a filtered-out or auto-approved row gets no value written."""
    return [
        {"name": target, "type": target_type, "nullable": True},
        {"name": "decision", "type": "str"},
        {"name": "reviewer_id", "type": "str", "nullable": True},
        {"name": "reviewed_at", "type": "str", "nullable": True},
        {"name": "review_notes", "type": "str", "nullable": True},
    ]
