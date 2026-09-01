from __future__ import annotations

import pandas as pd

from app.runtime.runner import execute_run, resume_run
from app.services.project import save_working_copy_as_version
from conftest import pinned_stages, resumed_stages
from run_seed import read_manifest
from stage_seed import add_stage

_COLUMNS = [{"name": "x", "type": "int", "nullable": True}]


def _add_source(project_dir) -> None:
    data_path = project_dir / "items.csv"
    pd.DataFrame({"x": [1, 2, 3]}).to_csv(data_path, index=False)
    add_stage(project_dir, {
        "id": "load",
        "description": "Load",
        "type": "input_data",
        "connector": {
            "kind": "file",
            "params": {"path": str(data_path), "format": "csv"},
        },
        "signature": {"form": "replaces", "produces": _COLUMNS},
    })


def _add_row_stage(project_dir) -> None:
    add_stage(project_dir, {
        "id": "map",
        "description": "Map",
        "type": "python_row_function",
        "cache": True,
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _COLUMNS}],
        },
        "function": {
            "kind": "inline",
            "code": "def transform(row):\n    return row\n",
        },
    })


def _find_stage(manifest, stage_id: str):
    return next(
        record for record in manifest["stage_records"]
        if record["stage_id"] == stage_id
    )


def test_row_progress_is_persisted_when_every_row_comes_from_cache(tmp_path):
    _add_source(tmp_path)
    _add_row_stage(tmp_path)
    save_working_copy_as_version(tmp_path.name, message="seed")

    execute_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path))
    replayed = execute_run(
        tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path)
    )

    record = _find_stage(replayed, "map")
    assert record["cached_rows"] == 3
    assert record["progress"] | {"updated_at": "ignored"} == {
        "completed": 3,
        "total": 3,
        "updated_at": "ignored",
    }
    stored = read_manifest(tmp_path, replayed["run_id"])
    assert _find_stage(stored, "map")["progress"]["completed"] == 3


def test_resume_starts_a_new_progress_sequence_for_the_rerun(tmp_path):
    _add_source(tmp_path)
    marker = tmp_path / "first-attempt-finished"
    code = (
        "def transform(df, *, progress):\n"
        "    from pathlib import Path\n"
        f"    marker = Path({str(marker)!r})\n"
        "    if not marker.exists():\n"
        "        progress(completed=1, total=2)\n"
        "        marker.write_text('retry')\n"
        "        raise RuntimeError('retry me')\n"
        "    progress(completed=0, total=2)\n"
        "    progress(completed=2, total=2)\n"
        "    return df\n"
    )
    add_stage(tmp_path, {
        "id": "shape",
        "description": "Shape",
        "type": "python_frame_function",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "load", "columns": _COLUMNS}],
            "produces": _COLUMNS,
        },
        "function": {"kind": "inline", "code": code},
    })
    save_working_copy_as_version(tmp_path.name, message="seed")

    first = execute_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path))
    assert first["status"] == "errors"
    assert _find_stage(first, "shape")["progress"]["completed"] == 1

    resumed = resume_run(
        tmp_path / "runs" / first["run_id"],
        tmp_path.name,
        first["run_id"],
        *resumed_stages(tmp_path, first["run_id"]),
    )

    assert resumed["status"] == "ok"
    assert _find_stage(resumed, "shape")["progress"]["completed"] == 2
