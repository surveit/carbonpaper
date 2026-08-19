"""A deploy kills the thread executing a run. What the reader sees afterwards.

The run record is left saying `running`, which is TRUE — the run is not over. So
recovery restarts the work rather than writing a terminal status; the only thing
that buries a run is exhausting its attempts, and that is reported.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.run_status import RunStatus, StageStatus
from app.runtime.stages import HANDLERS
from app.models.stage import StageType
from app.runtime.stages import llm_transform as lt
from app.services import run_recovery
from app.services.run_recovery import (
    MAX_RECOVERY_ATTEMPTS,
    find_interrupted_runs,
    resume_interrupted_runs,
)
from conftest import pinned_stages
from run_seed import read_manifest

_ROWS = [{"post_id": f"p{i}", "text": f"t{i}"} for i in range(8)]
_LOADED = [{"name": "post_id", "type": "str", "nullable": True},
           {"name": "text", "type": "str", "nullable": True}]
_SCORED = [*_LOADED, {"name": "label", "type": "str", "nullable": True}]


class _Calls:
    """The model seam. `fault` fires on the (1-based) call named by `fail_on`."""

    def __init__(self, fail_on: int | None = None, fault: BaseException | None = None):
        self.n, self.fail_on, self.fault = 0, fail_on, fault

    def __call__(self, stage_id, llm, row, *, reply_model, usage_out=None, **kw):
        self.n += 1
        if self.n == self.fail_on and self.fault is not None:
            raise self.fault
        return {"label": f"L-{row['text']}"}


def _write_project(root: Path) -> None:
    import pandas as pd
    from app.services import project as project_service
    from app.services import versioning
    from stage_seed import add_stage

    (root / "data").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_ROWS).to_csv(root / "data" / "posts.csv", index=False)
    add_stage(root, {
        "id": "load", "description": "Load posts", "type": "input_data",
        "connector": {"kind": "file", "params": {
            "path": str(root / "data" / "posts.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _LOADED}})
    add_stage(root, {
        "id": "score", "description": "Score posts", "type": "llm_transform",
        "inputs": [{"id": "load"}],
        "signature": {"form": "extends",
                      "reads": [{"input": "load", "columns": [_LOADED[1]]}],
                      "adds": [_SCORED[2]]},
        "llm": {"prompt_data_template": "score {text}", "batch_size": 1, "max_retries": 0}})
    add_stage(root, {
        "id": "tally", "description": "Tally labels", "type": "python_frame_function",
        "inputs": [{"id": "score"}],
        "signature": {"form": "replaces",
                      "reads": [{"input": "score", "columns": _SCORED}],
                      "produces": _SCORED},
        "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"}})
    version = project_service.save_working_copy_as_version(
        root.name, message="run recovery", reviewer="test")
    versioning.publish_version(root.name, version.version_id, reviewer="human")


@pytest.fixture(autouse=True)
def one_row_at_a_time():
    handler = HANDLERS[StageType.llm_transform]
    was, handler.parallelism = handler.parallelism, 1
    yield
    handler.parallelism = was


@pytest.fixture(autouse=True)
def sole_executor(monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_SOLE_EXECUTOR", "1")


@pytest.fixture(autouse=True)
def run_inline(monkeypatch):
    """The seam app.services.run documents for this: run the resume inline."""
    monkeypatch.setattr(
        "app.services.run._run_in_background",
        lambda target, *args: target(*args),
    )


def _project_dir(tmp_path: Path) -> Path:
    """Under the workspace root, because `resolve_run_dir` resolves a run there."""
    root = tmp_path / "examples" / "demo"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _kill_a_run_mid_stage(tmp_path: Path, monkeypatch) -> str:
    tmp_path = _project_dir(tmp_path)
    _write_project(tmp_path)
    monkeypatch.setattr(lt, "call_llm", _Calls(fail_on=6, fault=KeyboardInterrupt()))
    workflow, version = pinned_stages(tmp_path)
    from app.runtime.runner import execute_run
    with pytest.raises(KeyboardInterrupt):
        execute_run(tmp_path / "runs", tmp_path.name, workflow, version)
    return sorted(p.name for p in (tmp_path / "runs").iterdir())[-1]


def test_a_killed_run_is_picked_back_up_and_finishes(tmp_path, monkeypatch):
    run_id = _kill_a_run_mid_stage(tmp_path, monkeypatch)
    assert read_manifest(_project_dir(tmp_path), run_id)["status"] == RunStatus.RUNNING

    resumed = _Calls()
    monkeypatch.setattr(lt, "call_llm", resumed)
    resume_interrupted_runs()

    after = read_manifest(_project_dir(tmp_path), run_id)
    assert after["status"] == RunStatus.OK
    assert {r["stage_id"]: r["status"] for r in after["stage_records"]} == {
        "load": StageStatus.OK, "score": StageStatus.OK, "tally": StageStatus.OK}
    assert resumed.n == 3            # only the rows in flight when it died
    assert after["recovery_attempts"] == 1


def test_the_run_is_never_marked_dead_on_the_way(tmp_path, monkeypatch):
    """The reader's page keeps saying `running`, because that is what is true."""
    _kill_a_run_mid_stage(tmp_path, monkeypatch)
    seen: list[str] = []
    real_write = run_recovery.write_manifest
    monkeypatch.setattr(run_recovery, "write_manifest",
                        lambda m: (seen.append(m.status), real_write(m))[1])

    monkeypatch.setattr(lt, "call_llm", _Calls())
    resume_interrupted_runs()

    assert RunStatus.ERRORS not in seen and RunStatus.CANCELLED not in seen


def test_a_run_that_keeps_dying_is_abandoned_and_says_so(tmp_path, monkeypatch):
    """Silence would be worse than the burial: the page would spin forever."""
    run_id = _kill_a_run_mid_stage(tmp_path, monkeypatch)
    manifest = read_manifest(_project_dir(tmp_path), run_id)
    manifest["recovery_attempts"] = MAX_RECOVERY_ATTEMPTS
    from run_seed import store_manifest
    store_manifest(_project_dir(tmp_path), run_id, manifest)

    never = _Calls()
    monkeypatch.setattr(lt, "call_llm", never)
    resume_interrupted_runs()

    after = read_manifest(_project_dir(tmp_path), run_id)
    assert after["status"] == RunStatus.ERRORS
    assert never.n == 0
    dead = [r for r in after["stage_records"] if r["stage_id"] == "score"][0]
    assert dead["status"] == StageStatus.ERROR
    assert "Run abandoned" in dead["error"]["message"]


def test_nothing_is_resumed_unless_this_process_is_the_sole_executor(tmp_path, monkeypatch):
    """Two executors over one store would both run it; only a deployment can claim otherwise."""
    run_id = _kill_a_run_mid_stage(tmp_path, monkeypatch)
    monkeypatch.delenv("CARBON_PAPER_SOLE_EXECUTOR", raising=False)

    never = _Calls()
    monkeypatch.setattr(lt, "call_llm", never)
    resume_interrupted_runs()

    assert never.n == 0
    assert read_manifest(_project_dir(tmp_path), run_id)["status"] == RunStatus.RUNNING
    assert len(find_interrupted_runs()) == 1        # found, deliberately not touched
