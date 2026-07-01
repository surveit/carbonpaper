"""Integration: the runner's generic `limit:` truncation + manifest persistence.

Builds a one-stage file-connector methodology in a tmp dir, runs it, and checks
that `limit:` truncated the output, that the truncation was recorded (not
silent), and that manifest.json landed on disk.
"""
from __future__ import annotations

import json

import pandas as pd
import yaml

from app.runtime.runner import execute_run


def _make_methodology(root):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"name": [f"row{i}" for i in range(5)], "val": list(range(5))}) \
        .to_csv(root / "data" / "items.csv", index=False)
    stage = {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": "data/items.csv", "format": "csv"}},
        "limit": 2,
    }
    (root / "compiled" / "01_load.yaml").write_text(yaml.safe_dump(stage), encoding="utf-8")


def test_limit_truncates_and_is_recorded(tmp_path):
    _make_methodology(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path)

    assert manifest["status"] == "ok"
    [rec] = manifest["stages"]
    assert rec["status"] == "ok"
    assert rec["rows"] == 2                                   # truncated from 5
    assert any("truncated" in n for n in rec.get("notes", []))   # not silent

    run_dir = tmp_path / "runs" / manifest["run_id"]
    out = pd.read_parquet(run_dir / "outputs" / "load.parquet")
    assert len(out) == 2

    on_disk = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["run_id"] == manifest["run_id"]
    assert on_disk["status"] == "ok"
