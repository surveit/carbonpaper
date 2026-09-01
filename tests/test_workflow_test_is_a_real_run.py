"""Workflow test = a real run under runs/, marked is_test_run: it may READ the
stage-result cache (fast replay) but writes NO entries, so a later production run
is unaffected by having run one. Evidence is the probe-file technique
tests/test_run_cache_e2e.py uses: the `cache: true` stage appends a line each time
its body actually runs, so a cache hit leaves the probe untouched."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

from app.runtime.runner import execute_run
from app.services import workspace
from app.services import project as project_service
from app.services.workflow_test import run_workflow_test
from conftest import pinned_stages
from stage_seed import add_stage

_ROWS = [{"name": "a", "val": 1}, {"name": "b", "val": 2}, {"name": "c", "val": 3}]
_LOADED = [{"name": "name", "type": "str", "nullable": True}, {"name": "val", "type": "int", "nullable": True}]
_DOUBLED = [*_LOADED, {"name": "doubled", "type": "int", "nullable": True}]


def _clean_code(probe: Path) -> str:
    return (
        "def transform(row):\n"
        f"    with open({json.dumps(str(probe))}, 'a', encoding='utf-8') as h:\n"
        "        h.write('clean\\n')\n"
        "    return {**row, 'doubled': row['val'] * 2}\n"
    )


def _write_rows(root: Path, rows: list[dict[str, object]]) -> None:
    pd.DataFrame(rows).to_csv(root / "data" / "items.csv", index=False)


def _write_project(root: Path) -> Path:
    probe = root / "probe.log"
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    _write_rows(root, _ROWS[:2])
    add_stage(root, {
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file", "params": {
            "path": str(root / "data" / "items.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _LOADED},
    })
    add_stage(root, {
        "id": "clean", "description": "Clean", "type": "python_row_function",
        "inputs": [{"id": "load"}], "cache": True,
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _LOADED}],
            "adds": [{"name": "doubled", "type": "int", "nullable": True}],
        },
        "function": {"kind": "inline", "code": _clean_code(probe)},
    })
    return probe


def _publish(root: Path) -> str:
    version = project_service.save_working_copy_as_version(root.name, message="e2e")
    return version.version_id


def _invocations(probe: Path) -> Counter[str]:
    if not probe.exists():
        return Counter()
    return Counter(probe.read_text(encoding="utf-8").split())


@pytest.fixture
def project(tmp_path, monkeypatch):
    workspace.set_projects_dir(tmp_path)
    root = tmp_path / "demo"
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_workflow_test_replays_cached_rows_and_writes_no_new_entries(project):
    probe = _write_project(project)
    _publish(project)

    manifest = execute_run(project / "runs", project.name, *pinned_stages(project))
    assert manifest["status"] == "ok"
    assert _invocations(probe) == Counter({"clean": 2})

    result = run_workflow_test(project.name)
    assert result["ok"] is True
    assert _invocations(probe) == Counter({"clean": 2})  # no new invocation


def test_a_workflow_test_does_not_leak_its_computed_rows_into_the_production_cache(project):
    probe = _write_project(project)
    _publish(project)
    execute_run(project / "runs", project.name, *pinned_stages(project))
    assert _invocations(probe) == Counter({"clean": 2})  # "a", "b"

    _write_rows(project, _ROWS)  # add "c" — the workflow test's slice sees it
    result = run_workflow_test(project.name)
    assert result["ok"] is True
    assert _invocations(probe) == Counter({"clean": 3})  # only "c" recomputed

    manifest = execute_run(project / "runs", project.name, *pinned_stages(project))
    assert manifest["status"] == "ok"
    # "a"/"b" still replay from the FIRST production run's cache, but "c"
    # recomputes a SECOND time here — proof the workflow test recorded nothing
    # for it in the stage-result cache.
    assert _invocations(probe) == Counter({"clean": 4})
