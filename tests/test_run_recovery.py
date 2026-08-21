"""A deploy kills the thread executing a run. What the reader sees afterwards.

The record is left saying `running`, which is TRUE, so recovery restarts the work rather
than writing a terminal status. What licenses the restart is the run's EXPIRED lease, not
an assumption about how many machines are serving.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.persistence import get_store
from app.core.sqlite_store import SqliteKvStore
from app.core.run_status import RunStatus, StageStatus
from app.models.stage import StageType
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt
from app.services.run_recovery import (
    MAX_TENURES,
    find_interrupted_runs,
    find_ownerless_runs,
    load_production_runs,
    restart_interrupted_runs,
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
def restart_inline(monkeypatch):
    """The sweep's own thread is not the behaviour under test; its decisions are."""
    monkeypatch.setattr("app.services.run_recovery.run_in_background",
                        lambda work: work())


def _project_dir(tmp_path: Path) -> Path:
    root = tmp_path / "examples" / "demo"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sqlite() -> SqliteKvStore:
    store = get_store()
    assert isinstance(store, SqliteKvStore)
    return store


def _expire_the_lease(run_key: str) -> None:
    """Time passing. The sweep restarts an EXPIRED tenure, never a live one."""
    store = _sqlite()
    store._conn.execute(
        "UPDATE run_lease SET expires_at = unixepoch() - 1 WHERE run_id=?", (run_key,))
    store._conn.commit()


def _kill_a_run_mid_stage(tmp_path: Path) -> tuple[str, str]:
    # `release_lease` is stubbed for the kill: a daemon thread dying with its process
    # never unwinds, so the lease it held is left behind — the state the sweep reads.
    """Run until the 6th model call, then die the way a replaced process does."""
    project = _project_dir(tmp_path)
    _write_project(project)
    # Its own context, NOT the test's monkeypatch: undoing these must not also undo the
    # autouse fixtures, which would put the restart back on a real thread mid-assert.
    with pytest.MonkeyPatch.context() as kill:
        kill.setattr(lt, "call_llm", _Calls(fail_on=6, fault=KeyboardInterrupt()))
        kill.setattr(type(get_store()), "release_lease", lambda self, lease: None)
        workflow, version = pinned_stages(project)
        from app.runtime.runner import execute_run
        with pytest.raises(KeyboardInterrupt):
            execute_run(project / "runs", project.name, workflow, version)

    run_id = sorted(p.name for p in (project / "runs").iterdir())[-1]
    return run_id, f"{project.name}/runs/{run_id}"


def test_a_killed_run_is_restarted_and_finishes(tmp_path, monkeypatch):
    run_id, run_key = _kill_a_run_mid_stage(tmp_path)
    assert read_manifest(_project_dir(tmp_path), run_id)["status"] == RunStatus.RUNNING
    _expire_the_lease(run_key)

    restarted = _Calls()
    monkeypatch.setattr(lt, "call_llm", restarted)
    restart_interrupted_runs()

    after = read_manifest(_project_dir(tmp_path), run_id)
    assert after["status"] == RunStatus.OK
    assert {r["stage_id"]: r["status"] for r in after["stage_records"]} == {
        "load": StageStatus.OK, "score": StageStatus.OK}
    assert restarted.n == 3, "the row cache should have spared the five rows already scored"


def test_a_live_tenure_is_left_to_the_executor_that_holds_it(tmp_path, monkeypatch):
    """The unexpired case: someone may still be running this, so it is not ours to take."""
    run_id, _ = _kill_a_run_mid_stage(tmp_path)

    never = _Calls()
    monkeypatch.setattr(lt, "call_llm", never)
    assert find_interrupted_runs(load_production_runs()) == []
    restart_interrupted_runs()

    assert never.n == 0
    assert read_manifest(_project_dir(tmp_path), run_id)["status"] == RunStatus.RUNNING


def test_a_run_that_never_held_a_lease_is_surfaced_not_restarted(tmp_path, monkeypatch):
    """Records predating the lease. Nothing proved their executor dead, so a human decides."""
    run_id, run_key = _kill_a_run_mid_stage(tmp_path)
    _sqlite()._conn.execute("DELETE FROM run_lease WHERE run_id=?", (run_key,))
    _sqlite()._conn.commit()

    never = _Calls()
    monkeypatch.setattr(lt, "call_llm", never)
    restart_interrupted_runs()

    assert never.n == 0
    assert [m.run_id for m in find_ownerless_runs(load_production_runs())] == [run_id]
    assert read_manifest(_project_dir(tmp_path), run_id)["status"] == RunStatus.RUNNING


def test_the_run_is_never_marked_dead_on_the_way(tmp_path, monkeypatch):
    """The reader's page keeps saying `running`, because that is what is true."""
    run_id, run_key = _kill_a_run_mid_stage(tmp_path)
    _expire_the_lease(run_key)

    seen: list[str] = []
    from app.runtime import manifest as manifest_module
    real = manifest_module.write_manifest
    monkeypatch.setattr(manifest_module, "write_manifest",
                        lambda m: (seen.append(m.status), real(m))[1])
    monkeypatch.setattr(lt, "call_llm", _Calls())
    restart_interrupted_runs()

    assert RunStatus.ERRORS not in seen and RunStatus.CANCELLED not in seen


def test_a_run_that_keeps_dying_is_abandoned_and_says_so(tmp_path, monkeypatch):
    """Silence would be worse than the burial: the page would spin forever."""
    run_id, run_key = _kill_a_run_mid_stage(tmp_path)
    store = _sqlite()
    store._conn.execute(
        "UPDATE run_lease SET fence=?, expires_at=unixepoch()-1 WHERE run_id=?",
        (MAX_TENURES, run_key))
    store._conn.commit()

    never = _Calls()
    monkeypatch.setattr(lt, "call_llm", never)
    restart_interrupted_runs()

    after = read_manifest(_project_dir(tmp_path), run_id)
    assert after["status"] == RunStatus.ERRORS
    assert never.n == 0
    dead = [r for r in after["stage_records"] if r["stage_id"] == "score"][0]
    assert dead["status"] == StageStatus.ERROR
    assert "Run abandoned" in dead["error"]["message"]
