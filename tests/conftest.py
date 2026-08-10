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
    workflow_version = resolve_version_id(project_dir, version_id)
    return load_version_stages(project_dir, workflow_version), workflow_version


def resumed_stages(project_dir: Path, run_id: str) -> tuple[list[Stage], str]:
    workflow_version = read_run_manifest(project_dir / "runs" / run_id).workflow_version
    assert workflow_version, f"run {run_id} records no workflow_version"
    return load_version_stages(project_dir, workflow_version), workflow_version


def contribution_of(frame: pd.DataFrame) -> StageContribution:
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
    from app.services.workspace import set_projects_dir
    set_projects_dir(tmp_path / "examples")


@pytest.fixture
def projects_root(tmp_path, fresh_workspace):
    root = tmp_path / "examples"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture(autouse=True)
def reset_cancellation_registry():
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
    # `source` must name a column present on the frame the stage runs over, or the runtime raises.
    return {
        "reviewed_columns": {source: target},
        "verdict_column": "decision",
        "reviewer_column": "reviewer_id",
        "reviewed_at_column": "reviewed_at",
        "review_notes_column": "review_notes",
    }


def reads_of(input_id: str, columns: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{"input": input_id, "columns": list(columns)}]


def queue_added_columns(
    target: str = "human_score", target_type: str = "int"
) -> list[dict[str, object]]:
    return [
        {"name": target, "type": target_type, "nullable": True},
        {"name": "decision", "type": "str", "nullable": True},
        {"name": "reviewer_id", "type": "str", "nullable": True},
        {"name": "reviewed_at", "type": "str", "nullable": True},
        {"name": "review_notes", "type": "str", "nullable": True},
    ]


QUEUE_COLUMNS: dict[str, object] = queue_columns()

