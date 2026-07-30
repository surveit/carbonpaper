"""An un-stubbed `call_llm` raises `LLMError` rather than reaching the real `claude`
CLI; a test exercising the LLM boundary must monkeypatch it itself. The per-test
document and frame stores are configured ahead of the app's startup wiring, which
leaves an already-configured store alone."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.models import ReviewGuide, ReviewGuideStep
from app.runtime.context import (
    RunContext,
    RunIdentity,
)
from app.runtime.manifest import CONTRIBUTION_ATTR, StageContribution
from app.services import versioning
from app.core.stage_cache import ReadOnlyStageCache


def save_covering_guide(project_dir: Path, version_id: str) -> versioning.WorkflowVersion:
    """A guide narrating every stage of the version in one step — what publishing needs."""
    stages = versioning.load_version(project_dir, version_id).stages
    return versioning.save_version_guide(
        project_dir,
        version_id,
        ReviewGuide(steps=[ReviewGuideStep(
            title="How this workflow works",
            prose="Every stage, narrated together.",
            stage_ids=[stage.id for stage in stages],
        )]),
    )


def publish_with_guide(
    project_dir: Path, version_id: str, *, reviewer: str = "human"
) -> versioning.WorkflowVersion:
    """Publish past the review-guide gate, for a test whose subject is not the guide."""
    save_covering_guide(project_dir, version_id)
    return versioning.publish_version(project_dir, version_id, reviewer=reviewer)


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
    """A RunContext for tests that only care about a few of its fields. Project
    scope is whatever the caller passes: an `identity` with its `stage_cache`, or
    neither. A stage's telemetry is reported on its output frame's `.attrs`, not
    on the context, so there is nothing to seed here."""
    return RunContext(
        repo_root=repo_root, run_dir=run_dir,
        identity=identity, stage_cache=stage_cache,
        limits=dict(limits or {}), offsets=dict(offsets or {}),
        bust_cache=bust_cache,
    )
