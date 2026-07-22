"""app.runtime.preview.run_stage_preview: an in-memory, disk-write-free scratch
re-run of one stage on a caller-chosen subset of rows.

`run_stage_preview` takes no `project_dir` — see issue #185 / the arch test
app/runtime/_arch_tests/test_takes_objects_not_dirs.py. None of the
PREVIEWABLE_TYPES handlers read a project directory (only human_review_queue
and publish do, and both are refused before any handler runs), so the runtime
needs only `run_dir` (to load this run's upstream outputs) and `repo_root`."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.core.models import Stage
from app.runtime.errors import PreviewError
from app.runtime.preview import run_stage_preview


def _row_stage(stage_id: str = "t", inputs: list[str] | None = None) -> Stage:
    return Stage.model_validate({
        "id": stage_id,
        "name": stage_id,
        "type": "python_row_function",
        "inputs": [{"id": iid} for iid in (inputs or ["src"])],
        "function": {
            "kind": "inline",
            "code": "def transform(row):\n    return {**row, 'y': row['x'] * 10}\n",
        },
    })


def _write_output(run_dir: Path, stage_id: str, df: pd.DataFrame) -> str:
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    rel = f"outputs/{stage_id}.parquet"
    df.to_parquet(run_dir / rel, index=False)
    return rel


def test_run_stage_preview_runs_selected_rows_in_memory(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rel = _write_output(run_dir, "src", pd.DataFrame({"x": [1, 2, 3, 4]}))

    result = run_stage_preview(
        stage_def=_row_stage(),
        run_dir=run_dir,
        repo_root=tmp_path,
        output_by_id={"src": rel},
        selected_indices=[1, 3],
    )

    assert result["input_rows"] == 2
    assert result["selected_indices"] == [1, 3]
    assert [row["x"] for row in result["preview"]] == ["2", "4"]
    assert [row["y"] for row in result["preview"]] == ["20", "40"]


def test_run_stage_preview_writes_nothing_to_disk(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rel = _write_output(run_dir, "src", pd.DataFrame({"x": [1, 2]}))
    before = sorted(p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_file())

    run_stage_preview(
        stage_def=_row_stage(),
        run_dir=run_dir,
        repo_root=tmp_path,
        output_by_id={"src": rel},
        selected_indices=[0],
    )

    after = sorted(p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_file())
    assert after == before  # no manifest, output, or artifact appeared


def test_run_stage_preview_rejects_non_previewable_type(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rel = _write_output(run_dir, "src", pd.DataFrame({"x": [1]}))
    stage = Stage.model_validate({
        "id": "pub", "name": "pub", "type": "publish",
        "inputs": [{"id": "src"}],
        "publish": {},
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
    })

    with pytest.raises(PreviewError, match="can't be previewed"):
        run_stage_preview(
            stage_def=stage,
            run_dir=run_dir,
            repo_root=tmp_path,
            output_by_id={"src": rel},
            selected_indices=[0],
        )


def test_run_stage_preview_missing_upstream_output_raises(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with pytest.raises(PreviewError, match="no output in this run"):
        run_stage_preview(
            stage_def=_row_stage(),
            run_dir=run_dir,
            repo_root=tmp_path,
            output_by_id={"src": None},
            selected_indices=[0],
        )


def test_run_stage_preview_no_valid_indices_raises(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rel = _write_output(run_dir, "src", pd.DataFrame({"x": [1, 2]}))
    with pytest.raises(PreviewError, match="no valid row indices"):
        run_stage_preview(
            stage_def=_row_stage(),
            run_dir=run_dir,
            repo_root=tmp_path,
            output_by_id={"src": rel},
            selected_indices=[99],
        )


def test_run_stage_preview_takes_no_project_dir(tmp_path: Path) -> None:
    """Regression guard for #185: passing project_dir must be a TypeError, not
    silently accepted — this is what the arch test's allowlist deletion
    depends on staying true."""
    run_dir = tmp_path / "run"
    rel = _write_output(run_dir, "src", pd.DataFrame({"x": [1]}))
    with pytest.raises(TypeError):
        run_stage_preview(
            stage_def=_row_stage(),
            run_dir=run_dir,
            repo_root=tmp_path,
            project_dir=tmp_path,  # type: ignore[call-arg]
            output_by_id={"src": rel},
            selected_indices=[0],
        )
