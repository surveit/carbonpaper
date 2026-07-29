"""Per-row provenance for a stage whose output isn't row-preserving BY
POSITION (filter_rows, union) but whose per-row source IS exactly known. A
handler attaches two internal columns naming each row's source; the executor
splits them off before persisting the real output and writes them to a
sidecar file `app.runtime.trace` reads to cross that stage."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

TRACE_SOURCE_STAGE_KEY = "_trace_source_stage"
TRACE_SOURCE_ROW_KEY = "_trace_source_row"

_LINEAGE_COLUMNS = (TRACE_SOURCE_STAGE_KEY, TRACE_SOURCE_ROW_KEY)


def attach_row_provenance(
    df: pd.DataFrame, source_stage: list[str], source_row: list[int]
) -> pd.DataFrame:
    """`df` with the two internal lineage columns set from `source_stage` /
    `source_row`, one entry per row of `df`, in order."""
    out = df.copy()
    out[TRACE_SOURCE_STAGE_KEY] = source_stage
    out[TRACE_SOURCE_ROW_KEY] = source_row
    return out


def split_row_provenance(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """`df` with the internal lineage columns removed, and a sidecar frame
    carrying just those two columns (fresh row index) — or `df` unchanged and
    None when it carries no lineage columns at all."""
    present = [c for c in _LINEAGE_COLUMNS if c in df.columns]
    if not present:
        return df, None
    sidecar = df[list(_LINEAGE_COLUMNS)].reset_index(drop=True)
    return df.drop(columns=present), sidecar


def lineage_sidecar_path(run_dir: Path, stage_id: str) -> Path:
    """Where a stage's row-provenance sidecar lives, alongside its own output
    parquet in the run directory."""
    return Path(run_dir) / "outputs" / f"{stage_id}.lineage.parquet"
