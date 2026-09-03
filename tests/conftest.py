# No test reaches a live model. An un-stubbed `call_llm` raises `LLMError` rather than
# reaching the real `claude` CLI, and `offline_agent_sdk` below does the same for the
# chat seam; a test exercising either boundary must monkeypatch it itself. A test that
# genuinely wants a model is marked `live_llm`, deselected here and run by
# .github/workflows/live-llm-smoke.yml.
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from app.core.errors import LLMError
from app.core.stage_cache import ReadOnlyStageCache
from app.models import Stage, TableSchema, Workflow, WorkflowStage, WorkflowStageInput
from app.models.review_ledger import ReviewLedger
from app.models.stages.signature import promised_output_schema, transform_input_schemas
from app.models.stage_contribution import StageContribution
from app.runtime.manifest import read_run_manifest
from app.models.run_parameters import RunParameters
from app.core.frames import frame_to_table, table_to_frame
from app.runtime.stage_output import AwaitingReview, StageOutput
from app.runtime.context import (
    RunContext,
    RunIdentity,
)
from app.services.versioning import load_version_stages, resolve_version_id

_REVISION_0017 = "alembic/versions/0017_publish_stage_becomes_report.py"


def pinned_stages(project_dir: Path, version_id: str | None = None) -> tuple[Workflow, str]:
    workflow_version = resolve_version_id(project_dir.name, version_id)
    return _load_version_workflow(project_dir, workflow_version), workflow_version


def resumed_stages(project_dir: Path, run_id: str) -> tuple[Workflow, str]:
    workflow_version = read_run_manifest(project_dir.name, run_id).workflow_version
    assert workflow_version, f"run {run_id} records no workflow_version"
    return _load_version_workflow(project_dir, workflow_version), workflow_version


def _load_version_workflow(project_dir: Path, version_id: str) -> Workflow:
    return Workflow(stages=load_version_stages(project_dir.name, version_id))


def source_stage(stage_id: str, columns: list[dict[str, object]]) -> dict[str, object]:
    """A source supplying `columns`, so a stage under test has an upstream to read them from."""
    return {
        "id": stage_id,
        "type": "input_data",
        "description": f"rows for {stage_id}",
        "connector": {"kind": "file"},
        "signature": {"form": "replaces", "produces": list(columns)},
    }


def place_stage(stage: Stage, **input_schemas: object) -> WorkflowStage:
    """Each input supplies what the signature reads of it, unless this names a wider one."""
    reads = transform_input_schemas(stage)
    inputs = [
        WorkflowStageInput(
            id=ref.id,
            table_schema=(
                TableSchema.model_validate(input_schemas[ref.id])
                if ref.id in input_schemas
                else reads[ref.id]
            ),
        )
        for ref in stage.inputs
    ]
    return WorkflowStage(
        stage=stage, inputs=inputs,
        output_schema=promised_output_schema(stage, inputs),
    )


def as_inputs(frames: dict[str, pd.DataFrame]) -> dict[str, pa.Table]:
    """Handler inputs. Arrow is the wire format, so a test that builds pandas says so here."""
    return {name: frame_to_table(frame) for name, frame in frames.items()}


def rows_of(output: StageOutput) -> pd.DataFrame:
    """A handler's output as pandas, for a test that asserts on rows."""
    return table_to_frame(output.table)


def contribution_of(output: StageOutput) -> StageContribution:
    """Read off the returned StageOutput — the executor merges it, the context never holds it."""
    return output.contribution


def require_awaiting_review(output: StageOutput | None) -> AwaitingReview:
    assert output is not None and output.awaiting_review is not None, (
        "the stage finished; nothing was queued for review")
    return output.awaiting_review


@pytest.fixture(autouse=True)
def offline_llm(monkeypatch):
    monkeypatch.setattr("app.runtime.options.agent_available", lambda: False)


@pytest.fixture(autouse=True)
def offline_agent_sdk(monkeypatch, request):
    if request.node.get_closest_marker("live_llm"):
        return

    async def refuse(*, prompt, options):
        raise LLMError(
            "a test reached the live Claude Agent SDK. Ask for the "
            "`scripted_agent_turn` fixture to complete a turn without a model, or "
            "mark the test `live_llm` to run it in the smoke workflow."
        )
        yield  # never runs: what makes refuse the async generator query() is

    monkeypatch.setattr("app.core.agent.sdk_engine.query", refuse)


@pytest.fixture
def scripted_agent_turn(monkeypatch):
    """A turn that completes without a model, so what the page draws is assertable."""
    async def turn(self, prompt, *, message_history, emit, resume=None):
        return [
            {"role": "user", "parts": [{"type": "text", "text": prompt}]},
            {"role": "assistant", "parts": [{"type": "text", "text": "ok"}]},
        ], None

    monkeypatch.setattr(
        "app.core.agent.sdk_engine.ClaudeAgentSdkEngine.stream_turn", turn)


@pytest.fixture(autouse=True)
def fresh_store():
    from app.core.persistence import configure_store
    from app.core.sqlite_store import SqliteKvStore
    configure_store(SqliteKvStore(":memory:"))


@pytest.fixture(autouse=True)
def fresh_frame_store(tmp_path):
    from app.core.frames import FrameStore, configure_frame_store
    configure_frame_store(FrameStore(tmp_path / "frames"))


@pytest.fixture(autouse=True)
def fresh_file_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    # files_root() reads the environment, not the in-memory store `fresh_store` swaps in,
    # so without this an uploading test writes into the machine's real store.


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
    run_dir: Path = Path("."),
    identity: RunIdentity | None = None,
    stage_cache: ReadOnlyStageCache | None = None,
    decisions: ReviewLedger | None = None,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bust_cache: bool = False,
    queue_auto_approve: bool = False,
) -> RunContext:
    # Built here so a caller naming `identity` need not also pass a matching ledger.
    if decisions is None and identity is not None:
        decisions = ReviewLedger(identity.project)
    return RunContext(
        run_dir=run_dir,
        identity=identity, stage_cache=stage_cache, decisions=decisions,
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



def apply_0017_rename(spec: dict[str, object]) -> dict[str, object]:
    """0017 runs after 0006 and 0012, whose fixtures still spell the type `publish`."""
    renamed = {**spec}
    _revision_0017()._rename_one_spec(renamed, "publish", "report")
    return renamed


def _revision_0017() -> Any:
    path = Path(__file__).resolve().parents[1] / _REVISION_0017
    spec = importlib.util.spec_from_file_location("_rev_0017", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def drop_input_schemas(spec: dict[str, object]) -> dict[str, object]:
    """A stage no longer holds one; removing it from STORED payloads is a later migration's job."""
    inputs = spec.get("inputs")
    if not isinstance(inputs, list):
        return spec
    return {**spec, "inputs": [
        {"id": ref["id"]} if isinstance(ref, dict) else ref for ref in inputs
    ]}
