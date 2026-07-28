"""Frames are addressed by (collection, id) like the document store, but stored one
parquet file per frame under a root directory. Also owns the value-level pandas
knowledge the stage cache keys under - null forms, numpy scalars, frame identity."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any
import json
import math

import numpy as np
import pandas as pd
import pyarrow.lib as pa_lib

from app.core.errors import FrameNotSerializableError
from app.core.persistence import validate_id
from app.core.utils import compute_short_hash

# The on-disk extension for a frame file, named so every reader that
# distinguishes a parquet output from a csv one (by `Path.suffix`) compares
# against the same value instead of re-typing the literal.
PARQUET_SUFFIX = ".parquet"


def list_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """`frame` as one dict per row, column label → cell value. The labels are
    pinned to `str`: pandas types them as `Hashable`, so a caller that keys a row
    by a column name would otherwise be working against a wider type than any
    frame read from parquet or CSV actually carries."""
    return [
        {str(label): value for label, value in record.items()}
        for record in frame.to_dict("records")
    ]


def collapse_null_forms(value: object) -> object:
    """`value`, or None if `value` is one of the four pandas null forms a row
    cell can carry: plain `None`, `float('nan')`, `pd.NA`, or `pd.NaT` — all
    become None so a parquet round trip can't shift a row's identity;
    everything else passes through unchanged. Each form is tested
    individually — an identity check for None/pd.NA/pd.NaT, an explicit
    isinstance+isnan for a float nan — rather than via a single `pd.isna` call:
    pandas-stubs' `isna` overloads do not accept a bare `object` argument, and
    calling it on an array-valued cell (list/tuple/dict/set) would return an
    elementwise array whose truth value is ambiguous in a plain `if`. None of
    the checks here ask pd.isna anything, so an array-valued cell simply
    matches none of them and falls through to the final `return value`
    unchanged."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def convert_cell_to_json_native(value: object) -> object:
    """A `json.dumps` default for frame cells: a numpy scalar becomes its Python
    equivalent via `.item()` (so a numeric cell survives as a JSON number rather
    than a string), kept only when that equivalent is itself JSON-native;
    everything else — a pandas Timestamp, a numpy datetime, an arbitrary object
    — is stringified."""
    if isinstance(value, np.generic):
        native = value.item()
        if native is None or isinstance(native, (bool, int, float, str)):
            return native
        return str(native)
    return str(value)


def compute_frames_fingerprint(frames: Sequence[pd.DataFrame]) -> str:
    """One identity for an ordered sequence of frames — a frame-shaped stage's
    inputs in its declared input order. Order matters: swapping a join's two
    sides is a different input."""
    return compute_short_hash(
        json.dumps([compute_frame_fingerprint(frame) for frame in frames])
    )


def compute_frame_fingerprint(frame: pd.DataFrame) -> str:
    """compute_short_hash over the canonical JSON of a WHOLE frame: its column
    labels in their own order, then its cells row by row in their own order,
    each cell collapsed through `collapse_null_forms` exactly as a row cell is.

    Column and row ORDER are part of the identity here, unlike a row
    fingerprint, where key order is deliberately irrelevant: a whole-frame
    transform may index positionally or depend on sort order, so a reordered
    input is a genuinely different input and must not resolve to the same
    cached output. The frame's index is not part of the identity — it does not
    survive the parquet round trip the payload takes."""
    canonical = {
        "columns": [str(label) for label in frame.columns],
        "rows": [
            [collapse_null_forms(cell) for cell in row]
            for row in frame.itertuples(index=False, name=None)
        ],
    }
    return compute_short_hash(json.dumps(canonical, separators=(",", ":"), default=str))


class FrameStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, collection: str, id: str) -> Path:
        validate_id(collection)
        validate_id(id)
        return self.root / collection / f"{id}.parquet"

    def save_frame(
        self, collection: str, id: str, frame: pd.DataFrame, *, overwrite: bool = True
    ) -> None:
        path = self._path(collection, id)
        if path.exists() and not overwrite:
            raise FileExistsError(f"frame already exists: {collection}/{id}")
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)

    def load_frame(self, collection: str, id: str) -> pd.DataFrame | None:
        path = self._path(collection, id)
        if not path.exists():
            return None
        return pd.read_parquet(path)

    def exists(self, collection: str, id: str) -> bool:
        return self._path(collection, id).exists()

    def delete(self, collection: str, id: str) -> None:
        self._path(collection, id).unlink(missing_ok=True)


_frame_store: FrameStore | None = None


def configure_frame_store(store: FrameStore) -> None:
    """Install the process-wide frame store. App startup calls this once with a
    FrameStore under the workspace; each test installs a fresh one rooted at its
    own tmp dir."""
    global _frame_store
    _frame_store = store


def get_frame_store() -> FrameStore:
    if _frame_store is None:
        raise RuntimeError("frame store not configured; call configure_frame_store() first")
    return _frame_store


def is_frame_store_configured() -> bool:
    return _frame_store is not None


def save_frame_or_reject(
    collection: str, id: str, frame: pd.DataFrame, *, described_as: str
) -> None:
    """Save `frame` to the configured store, raising `FrameNotSerializableError`
    — prefixed `described_as` — for a dtype/shape parquet cannot represent,
    after removing whatever partial file the failed write left, so a later read
    never resolves to a truncated frame. A disk/OS error is deliberately NOT
    converted: it propagates."""
    store = get_frame_store()
    try:
        store.save_frame(collection, id, frame)
    except (pa_lib.ArrowException, ValueError, TypeError) as exc:
        store.delete(collection, id)
        raise FrameNotSerializableError(
            f"{described_as}: output frame could not be written as parquet ({exc})"
        ) from exc
