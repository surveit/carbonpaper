# An un-stubbed `call_llm` raises `LLMError` rather than reaching the real `claude`
# CLI; a test exercising the LLM boundary must monkeypatch it itself.
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.core.stage_cache import ReadOnlyStageCache
from app.models import Stage
from app.models.run_manifest import StageContribution, read_run_manifest
from app.models.run_parameters import RunParameters
from app.runtime.context import (
    RunContext,
    RunIdentity,
)
from app.runtime.manifest import CONTRIBUTION_ATTR
from app.services.versioning import load_version_stages, resolve_version_id


def pinned_stages(project_dir: Path, version_id: str | None = None) -> tuple[list[Stage], str]:
    """The (stages, version id) a run pins, for splatting into the runner's entry points."""
    # What every production caller now composes before calling in: resolve which
    # version to pin, then load that snapshot. The runner reads no versions itself.
    workflow_version = resolve_version_id(project_dir, version_id)
    return load_version_stages(project_dir, workflow_version), workflow_version


def resumed_stages(project_dir: Path, run_id: str) -> tuple[list[Stage], str]:
    """The (stages, version id) a resume must execute: the version THIS run pinned."""
    # Read off the manifest, not the newest published version, so a resume stays on
    # the snapshot the halted run started on even if a newer one was published since.
    workflow_version = read_run_manifest(project_dir / "runs" / run_id).workflow_version
    assert workflow_version, f"run {run_id} records no workflow_version"
    return load_version_stages(project_dir, workflow_version), workflow_version


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
def fresh_workspace(tmp_path):
    """Each test gets its own projects root, so nothing reads or writes the real
    examples/. There is only ever ONE workspace in a process — this points it at
    a temp dir, exactly as fresh_store points the document store at :memory:.
    A test that wants the directory itself takes the `projects_root` fixture.

    The directory is NOT created here: an absent projects root is a real state
    the code already handles (list_project_names returns [], create_project
    mkdirs its parents), and leaving it uncreated keeps tests that stage the
    directory themselves working unchanged."""
    from app.services.workspace import set_projects_dir
    set_projects_dir(tmp_path / "examples")


@pytest.fixture
def projects_root(tmp_path, fresh_workspace):
    """The temp projects root fresh_workspace configured — for a test that needs
    to stage a project directory on disk or assert on what was written. Created
    on demand, so taking this fixture also guarantees the directory exists."""
    root = tmp_path / "examples"
    root.mkdir(parents=True, exist_ok=True)
    return root


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
    queue_auto_approve: bool = False,
) -> RunContext:
    """A RunContext for tests that only care about a few of its fields. Project
    scope is whatever the caller passes: an `identity` with its `stage_cache`, or
    neither. A stage's telemetry is reported on its output frame's `.attrs`, not
    on the context, so there is nothing to seed here."""
    return RunContext(
        repo_root=repo_root, run_dir=run_dir,
        identity=identity, stage_cache=stage_cache,
        params=RunParameters(
            limits=dict(limits or {}), offsets=dict(offsets or {}),
            bust_cache=bust_cache, queue_auto_approve=queue_auto_approve,
            is_test_run=queue_auto_approve,
        ),
    )


def queue_columns(source: str = "score", target: str = "human_score") -> dict[str, object]:
    # The queue-column names a human_review_queue fixture declares when the test is about
    # something else (halting, caching, counts). `source` is the input column reviewed,
    # which must exist on the frame the fixture runs the stage over — the runtime raises
    # if it does not.
    return {
        "reviewed_columns": {source: target},
        "verdict_column": "decision",
        "reviewer_column": "reviewer_id",
        "reviewed_at_column": "reviewed_at",
        "review_notes_column": "review_notes",
    }


def queue_added_columns(
    target: str = "human_score", target_type: str = "int"
) -> list[dict[str, object]]:
    # The output_schema declarations `queue_columns()` obliges a fixture to make: a stage
    # must declare every column it adds, and every review-record column but the verdict
    # must be nullable (the runtime writes none of them into a skipped or auto-approved
    # row).
    return [
        {"name": target, "type": target_type, "nullable": True},
        {"name": "decision", "type": "str", "nullable": True},
        {"name": "reviewer_id", "type": "str", "nullable": True},
        {"name": "reviewed_at", "type": "str", "nullable": True},
        {"name": "review_notes", "type": "str", "nullable": True},
    ]


QUEUE_COLUMNS: dict[str, object] = queue_columns()

