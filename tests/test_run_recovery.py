"""A deploy kills the thread executing a run; what the reader sees. docs/run-leases.md"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.core.persistence import get_store
from app.core.sqlite_store import SqliteKvStore
from app.core.run_status import RunStatus, StageStatus
from app.models.stage import StageType
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt
from app.services import run_recovery
from app.services.run import expire_tenures_on_shutdown
from app.services.run_recovery import (
    MAX_TENURES,
    find_interrupted_runs,
    find_ownerless_runs,
    load_production_runs,
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

    def __init__(self, fail_on=None, fault=None, before_fault=None):
        self.n, self.fail_on, self.fault = 0, fail_on, fault
        self.before_fault = before_fault

    def __call__(self, stage_id, llm, row, *, reply_model, usage_out=None, **kw):
        self.n += 1
        if self.n == self.fail_on and self.fault is not None:
            if self.before_fault is not None:
                self.before_fault()
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
def resume_inline(monkeypatch):
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
    """Time passing. The sweep resumes an EXPIRED tenure, never a live one."""
    store = _sqlite()
    store._conn.execute(
        "UPDATE run_lease SET expires_at = unixepoch() - 1 WHERE run_id=?", (run_key,))
    store._conn.commit()


def _kill_a_run_mid_stage(tmp_path: Path, *, end_tenures: bool = False) -> tuple[str, str]:
    """Run until the 6th model call, then die the way a replaced process does."""
    project = _project_dir(tmp_path)
    _write_project(project)
    # Its own context: undoing these must not also undo the autouse fixtures.
    with pytest.MonkeyPatch.context() as kill:
        # SIGTERM arrives while the run still executes; the thread then dies unwinding nothing.
        kill.setattr(lt, "call_llm", _Calls(
            fail_on=6, fault=KeyboardInterrupt(),
            before_fault=expire_tenures_on_shutdown if end_tenures else None))
        kill.setattr(type(get_store()), "release_lease", lambda self, lease: None)
        workflow, version = pinned_stages(project)
        from app.runtime.runner import execute_run
        with pytest.raises(KeyboardInterrupt):
            execute_run(project / "runs", project.name, workflow, version)

    run_id = sorted(p.name for p in (project / "runs").iterdir())[-1]
    return run_id, f"{project.name}/runs/{run_id}"


def test_a_killed_run_is_resumed_and_finishes(tmp_path, monkeypatch):
    run_id, run_key = _kill_a_run_mid_stage(tmp_path)
    assert read_manifest(_project_dir(tmp_path), run_id)["status"] == RunStatus.RUNNING
    _expire_the_lease(run_key)

    resumed = _Calls()
    monkeypatch.setattr(lt, "call_llm", resumed)
    resume_interrupted_runs()

    after = read_manifest(_project_dir(tmp_path), run_id)
    assert after["status"] == RunStatus.OK
    assert {r["stage_id"]: r["status"] for r in after["stage_records"]} == {
        "load": StageStatus.OK, "score": StageStatus.OK}
    assert resumed.n == 3, "the row cache should have spared the five rows already scored"


def test_a_live_tenure_is_left_to_the_executor_that_holds_it(tmp_path, monkeypatch):
    """A boot cannot tell a run it orphaned from one a live peer is executing, so it waits."""
    run_id, _ = _kill_a_run_mid_stage(tmp_path)

    never = _Calls()
    monkeypatch.setattr(lt, "call_llm", never)
    assert find_interrupted_runs(load_production_runs()) == []
    resume_interrupted_runs()

    assert never.n == 0
    assert read_manifest(_project_dir(tmp_path), run_id)["status"] == RunStatus.RUNNING


def test_a_shutdown_ends_the_tenure_so_the_next_boot_resumes_at_once(tmp_path, monkeypatch):
    """The deploy case. Without this the run waits out the full TTL before anyone may take it."""
    run_id, run_key = _kill_a_run_mid_stage(tmp_path, end_tenures=True)

    assert [m.run_id for m, _ in find_interrupted_runs(load_production_runs())] == [run_id], (
        "a run whose process announced its own shutdown should be resumable immediately")
    resumed = _Calls()
    monkeypatch.setattr(lt, "call_llm", resumed)
    resume_interrupted_runs()

    assert read_manifest(_project_dir(tmp_path), run_id)["status"] == RunStatus.OK


def test_the_sweep_keeps_running_after_boot(tmp_path, monkeypatch):
    """A tenure expires minutes after a boot, so a boot-only sweep would never see it."""
    run_id, run_key = _kill_a_run_mid_stage(tmp_path)
    monkeypatch.setattr(lt, "call_llm", _Calls())
    monkeypatch.setattr(run_recovery, "SWEEP_EVERY_SECONDS", 0.05)

    stop = threading.Event()
    with pytest.MonkeyPatch.context() as real_thread:
        real_thread.setattr(run_recovery, "run_in_background", _in_a_thread)
        run_recovery.watch_for_interrupted_runs(stop)
        _expire_the_lease(run_key)                     # AFTER the first sweep has come and gone
        finished = _wait_until(
            lambda: read_manifest(_project_dir(tmp_path), run_id)["status"] == RunStatus.OK)
        stop.set()

    assert finished, "the run was still `running` — the sweep stopped after boot"


def _in_a_thread(work):
    threading.Thread(target=work, daemon=True).start()


def _wait_until(done, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(0.05)
    return False


def test_a_run_that_never_held_a_lease_is_surfaced_not_resumed(tmp_path, monkeypatch):
    """Records predating the lease. Nothing proved their executor dead, so a human decides."""
    run_id, run_key = _kill_a_run_mid_stage(tmp_path)
    _sqlite()._conn.execute("DELETE FROM run_lease WHERE run_id=?", (run_key,))
    _sqlite()._conn.commit()

    never = _Calls()
    monkeypatch.setattr(lt, "call_llm", never)
    resume_interrupted_runs()

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
    resume_interrupted_runs()

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
    resume_interrupted_runs()

    after = read_manifest(_project_dir(tmp_path), run_id)
    assert after["status"] == RunStatus.ERRORS
    assert never.n == 0
    dead = [r for r in after["stage_records"] if r["stage_id"] == "score"][0]
    assert dead["status"] == StageStatus.ERROR
    assert "Run abandoned" in dead["error"]["message"]
