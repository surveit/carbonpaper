"""Workflow test = a real run under runs/, marked is_test_run: it may READ the
stage-result cache (fast replay) but writes NO entries, so a later production run
is unaffected by having run one. Evidence is the same probe-file technique
tests/test_run_cache_e2e.py uses: a stage appends a line to a file each time its
body actually runs, so a cache hit leaves the probe untouched."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

from app.runtime.runner import execute_run
from app.services import versioning, workspace
from app.services.workflow_test import run_workflow_test
from conftest import publish_with_guide

_ROWS = [{"name": "a", "val": 1}, {"name": "b", "val": 2}, {"name": "c", "val": 3}]
_LOADED = [{"name": "name", "type": "str"}, {"name": "val", "type": "int"}]
_DOUBLED = [*_LOADED, {"name": "doubled", "type": "int"}]


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
    """`load` (csv) -> `clean` (row-mapped, cached). Returns the probe file
    `clean`'s body appends to."""
    probe = root / "probe.log"
    (root / "compiled").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    _write_rows(root, _ROWS[:2])
    (root / "compiled" / "01_load.json").write_text(json.dumps({
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file", "params": {
            "path": str(root / "data" / "items.csv"), "format": "csv"}},
        "output_schema": {"columns": _LOADED},
    }), encoding="utf-8")
    (root / "compiled" / "02_clean.json").write_text(json.dumps({
        "id": "clean", "name": "Clean", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": {"columns": _LOADED}}],
        "output_schema": {"columns": _DOUBLED},
        "function": {"kind": "inline", "code": _clean_code(probe)},
    }), encoding="utf-8")
    return probe


def _publish(root: Path) -> str:
    version = versioning.create_version_from_disk(root, message="e2e", reviewer="test")
    publish_with_guide(root, version.version_id, reviewer="human")
    return version.version_id


def _invocations(probe: Path) -> Counter[str]:
    if not probe.exists():
        return Counter()
    return Counter(probe.read_text(encoding="utf-8").split())


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    root = tmp_path / "demo"
    root.mkdir()
    return root


def test_workflow_test_replays_cached_rows_and_writes_no_new_entries(project):
    """A production run pins rows "a"/"b". A workflow test over the SAME rows
    replays both from the cache — `clean`'s body never runs again."""
    probe = _write_project(project)
    _publish(project)

    manifest = execute_run(project, repo_root=project)
    assert manifest["status"] == "ok"
    assert _invocations(probe) == Counter({"clean": 2})

    result = run_workflow_test(project.name)
    assert result["ok"] is True
    assert _invocations(probe) == Counter({"clean": 2})  # no new invocation


def test_production_run_after_a_workflow_test_is_unaffected(project):
    """The workflow test's slice includes a row ("c") no run has ever cached, so
    it must compute it. If that computation leaked into the cache, a later
    production run over the same row would replay it instead of recomputing —
    the poisoning this seam must prevent. Assert the opposite."""
    probe = _write_project(project)
    _publish(project)
    execute_run(project, repo_root=project)
    assert _invocations(probe) == Counter({"clean": 2})  # "a", "b"

    _write_rows(project, _ROWS)  # add "c" — the workflow test's slice sees it
    result = run_workflow_test(project.name)
    assert result["ok"] is True
    assert _invocations(probe) == Counter({"clean": 3})  # only "c" recomputed

    time.sleep(1.05)  # run ids are second-resolution
    manifest = execute_run(project, repo_root=project)
    assert manifest["status"] == "ok"
    # "a"/"b" still replay from the FIRST production run's cache, but "c"
    # recomputes a SECOND time here — proof the workflow test recorded nothing
    # for it in the stage-result cache.
    assert _invocations(probe) == Counter({"clean": 4})
