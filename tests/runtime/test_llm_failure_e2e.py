"""The same failures through `execute_run`, where a reviewer meets them.

A three-stage workflow: load -> score (llm_transform) -> tally. What is asserted
is what the operator gets back — the run's status, what the next run costs, and
whether the record says the run ended at all.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.runtime.runner import execute_run
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt
from app.models.stage import StageType
from app.services import project as project_service
from app.services import versioning
from conftest import pinned_stages
from run_seed import read_manifest
from stage_seed import add_stage

_ROWS = [{"post_id": f"p{i}", "text": f"t{i}"} for i in range(8)]
_LOADED = [{"name": "post_id", "type": "str", "nullable": True},
           {"name": "text", "type": "str", "nullable": True}]
_SCORED = [*_LOADED, {"name": "label", "type": "str", "nullable": True}]


@pytest.fixture(autouse=True)
def one_row_at_a_time():
    handler = HANDLERS[StageType.llm_transform]
    was = handler.parallelism
    handler.parallelism = 1
    yield
    handler.parallelism = was


def _write_project(root: Path) -> None:
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
        root.name, message="llm failure e2e", reviewer="test")
    versioning.publish_version(root.name, version.version_id, reviewer="human")


class _Calls:
    def __init__(self, fail_on: int | None = None, fault: BaseException | None = None):
        self.n, self.fail_on, self.fault = 0, fail_on, fault

    def __call__(self, stage_id, llm, row, *, reply_model, usage_out=None, **kw):
        self.n += 1
        if self.n == self.fail_on and self.fault is not None:
            raise self.fault
        return {"label": f"L-{row['text']}"}


def _run(project: Path) -> dict:
    return execute_run(project / "runs", project.name, *pinned_stages(project))


def _statuses(manifest: dict) -> dict[str, str]:
    return {r["stage_id"]: r["status"] for r in manifest["stage_records"]}


def test_one_failed_row_errors_the_stage_blocks_downstream_and_the_next_run_costs_one_call(
    tmp_path, monkeypatch
):
    _write_project(tmp_path)
    first = _Calls(fail_on=6, fault=RuntimeError("model refused"))
    monkeypatch.setattr(lt, "call_llm", first)

    manifest = _run(tmp_path)

    assert manifest["status"] == "errors"
    assert _statuses(manifest) == {"load": "ok", "score": "error", "tally": "pending"}
    assert first.n == 8

    second = _Calls()
    monkeypatch.setattr(lt, "call_llm", second)
    recovered = _run(tmp_path)

    assert second.n == 1                     # only the row that failed
    assert recovered["status"] == "ok"
    assert _statuses(recovered)["tally"] == "ok"


def test_a_run_aborted_mid_stage_is_left_recorded_as_running(tmp_path, monkeypatch):
    """No `except BaseException` on the path, so nothing marks the run terminal."""
    _write_project(tmp_path)
    calls = _Calls(fail_on=6, fault=KeyboardInterrupt())
    monkeypatch.setattr(lt, "call_llm", calls)

    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path)

    run_id = sorted(p.name for p in (tmp_path / "runs").iterdir())[-1]
    stored = read_manifest(tmp_path, run_id)
    assert stored["status"] == "running"
    assert _statuses(stored)["score"] == "running"

    second = _Calls()
    monkeypatch.setattr(lt, "call_llm", second)
    assert _run(tmp_path)["status"] == "ok"
    assert second.n == 3                     # the 5 answered before the abort were kept


def test_a_crashed_run_offers_only_cancel_on_its_page(tmp_path, monkeypatch):
    """`running` is read as in-flight, so the page offers the one control that needs a live process."""
    from app.web.run_header import choose_run_cta

    _write_project(tmp_path)
    monkeypatch.setattr(lt, "call_llm", _Calls(fail_on=6, fault=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path)

    run_id = sorted(p.name for p in (tmp_path / "runs").iterdir())[-1]
    cta = choose_run_cta(tmp_path.name, run_id, read_manifest(tmp_path, run_id))

    assert cta.primary is not None and cta.primary.url.endswith("/cancel")
    assert not [action for action in cta.secondary if action.url.endswith("/resume")]
