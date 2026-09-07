"""The decisions a run was handed, in its own directory. docs/run-manifest.md"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pyarrow as pa

from app.core.frames import read_frame_table, write_frame_table_with_csv_fallback
from app.core.json_types import JsonDict

DECISIONS_DIR = "review_decisions"
# The column carrying a row's identity; every other column is the decided row itself.
FINGERPRINT_COLUMN = "__input_fingerprint"


def write_run_decisions(
    run_dir: Path, stage_id: str, rows: Mapping[str, JsonDict]
) -> Path | None:
    """None where nothing was decided: an absent file and an empty one would read alike."""
    if not rows:
        return None
    table = pa.Table.from_pylist([
        {FINGERPRINT_COLUMN: fingerprint, **row} for fingerprint, row in rows.items()
    ])
    directory = run_dir / DECISIONS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return write_frame_table_with_csv_fallback(table, directory / f"{stage_id}.parquet").path


def read_run_decisions(run_dir: Path, stage_id: str) -> dict[str, JsonDict]:
    """{} where nothing was handed in — how a first run and a cache-busted one both look."""
    path = _find_decisions_file(run_dir, stage_id)
    if path is None:
        return {}
    table = read_frame_table(path)
    if FINGERPRINT_COLUMN not in table.column_names:
        raise ValueError(
            f"the decisions handed to stage '{stage_id}' carry no "
            f"'{FINGERPRINT_COLUMN}' column, so no row can be matched to one: {path}"
        )
    return {
        str(row.pop(FINGERPRINT_COLUMN)): row for row in table.to_pylist()
    }


def _find_decisions_file(run_dir: Path, stage_id: str) -> Path | None:
    for suffix in (".parquet", ".csv"):
        path = run_dir / DECISIONS_DIR / f"{stage_id}{suffix}"
        if path.exists():
            return path
    return None
