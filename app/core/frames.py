"""Frames are addressed by (collection, id) like the document store, but stored one
parquet file per frame under a root directory. Also owns the value-level pandas
knowledge the stage cache and the schema checks key under - null forms, numpy
scalars, extension dtypes, what a cell's Python type says about it, frame identity."""
from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, cast
import datetime as _dt
import json
import math

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.lib as pa_lib
import pyarrow.parquet as pq

from app.core.errors import FrameNotSerializableError
from app.core.persistence import validate_id
from app.core.utils import compute_short_hash

# The on-disk extension for a frame file, named so every reader that
# distinguishes a parquet output from a csv one (by `Path.suffix`) compares
# against the same value instead of re-typing the literal.
PARQUET_SUFFIX = ".parquet"


# ── Frame files THIS codebase wrote ──────────────────────────────────────────
# Stage outputs, the frame cache, lineage sidecars, node decisions, the review
# queue, eval results. The read is the exact inverse of the write: no schema, no
# coercion, no dtype argument reaches these. A frame that needs coercion on the
# way back in is a frame something already got wrong on the way out — see the
# `read_foreign_*` readers below for the other, genuinely different operation.


def read_frame_file(path: Path) -> pd.DataFrame:
    """Parquet, or the CSV a writer falls back to for a frame parquet cannot hold — by suffix."""
    return _read_frame_parquet(path) if path.suffix == PARQUET_SUFFIX else pd.read_csv(path)


def write_frame_file(frame: pd.DataFrame, path: Path) -> None:
    """The inverse of `read_frame_file`, dispatching on the same suffix. The index is never
    written."""
    if path.suffix == PARQUET_SUFFIX:
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False)


class FrameWrite(NamedTuple):
    path: Path
    # None when the parquet write succeeded; otherwise the reason it did not,
    # for a caller that reports the degradation to a reviewer.
    parquet_error: str | None


def write_frame_file_with_csv_fallback(frame: pd.DataFrame, path: Path) -> FrameWrite:
    """`path` as parquet, else its `.csv` sibling for a dtype/shape parquet cannot hold."""
    try:
        frame.to_parquet(path, index=False)
    # Mixed-type object columns and nested Python values are what CSV rescues,
    # by stringifying them. A disk/OS error (ENOSPC, permission) is deliberately
    # NOT caught: it would fail identically for CSV, so it propagates to the
    # caller rather than silently degrading the artifact.
    except (pa_lib.ArrowException, ValueError, TypeError) as exc:
        csv_path = path.with_suffix(".csv")
        frame.to_csv(csv_path, index=False)
        return FrameWrite(csv_path, str(exc))
    return FrameWrite(path, None)


def render_frame_as_csv_text(frame: pd.DataFrame) -> str:
    """`frame` as CSV text, no index — the same serialization `write_frame_file` puts on disk."""
    return frame.to_csv(index=False)


# ── FOREIGN files ────────────────────────────────────────────────────────────
# A file a user pointed us at, not one we wrote: an `input_data` source, an eval
# dataset. This is typed ingest, so a `dtype` pin IS correct here — a csv cell
# arrives as text and has to be typed. Which columns to pin is domain knowledge
# that reads a declared schema, and the "app.core does not import the domain
# models" contract keeps that above this layer: the caller works out the pins
# and the format and passes plain types down, so these readers hold the pandas
# call and nothing else.


def read_foreign_csv(path: Path, *, dtype: Mapping[Hashable, Any] | None = None) -> pd.DataFrame:
    return pd.read_csv(path, dtype=dtype)


# Parquet carries its own types, so who wrote the file changes nothing about how to
# read it: the same reader as `read_frame_file`. The dtype pins the other foreign
# readers take exist only because csv/xlsx/json hold no types to read.
def read_foreign_parquet(path: Path) -> pd.DataFrame:
    return _read_frame_parquet(path)


def read_foreign_json_lines(
    path: Path, *, dtype: Mapping[Hashable, Any] | None = None
) -> pd.DataFrame:
    return pd.read_json(path, lines=True, dtype=dtype)


def read_foreign_excel(
    path: Path, *, sheet_name: str | int, header_row: int,
    dtype: Mapping[Hashable, Any] | None = None,
) -> pd.DataFrame:
    """One sheet, so pandas hands back a frame — never the dict a None/list `sheet_name` returns."""
    frame = pd.read_excel(
        path, sheet_name=sheet_name, header=header_row, engine="openpyxl",
        # pandas types read_excel's `dtype` as Mapping[str, ...] and read_csv's
        # as Mapping[Hashable, ...]; the key is invariant, so one caller-side
        # mapping cannot satisfy both signatures without this cast.
        dtype=cast("Mapping[str, Any] | None", dtype),
    )
    assert isinstance(frame, pd.DataFrame)
    return frame


# The parquet read every frame this module hands back goes through, and the
# inverse of `frame.to_parquet`. `pd.read_parquet` is not that inverse: it
# materializes a written list cell as an `np.ndarray`, so a saved
# `["a", "b"]` returns as `array(["a", "b"])` and hashes through the
# `default=str` fallback in `compute_frame_fingerprint` /
# `compute_row_fingerprint` as `"['a' 'b']"` rather than as a JSON array —
# the same data under a different cache key. `types_mapper` fires only on the
# arrow LIST types, so every scalar column keeps the numpy-backed dtype and
# the exact cell type `pd.read_parquet` gives it, and only the list columns
# move. `use_pandas_metadata` matches what `pd.read_parquet` asks for.
def _read_frame_parquet(path: Path) -> pd.DataFrame:
    table = pq.read_table(path, use_pandas_metadata=True)
    return table.to_pandas(types_mapper=_map_list_type_to_arrow_dtype)


def _map_list_type_to_arrow_dtype(arrow_type: pa.DataType) -> pd.ArrowDtype | None:
    """None leaves the column on pandas' own default for that arrow type."""
    if (
        pa.types.is_list(arrow_type)
        or pa.types.is_large_list(arrow_type)
        or pa.types.is_fixed_size_list(arrow_type)
    ):
        return pd.ArrowDtype(arrow_type)
    return None


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


def is_null_form(value: object) -> bool:
    """Not a pd.isna test: an np.float32 nan or bare np.datetime64 NaT reads as non-null here."""
    return collapse_null_forms(value) is None


def is_sequence_cell(value: object) -> bool:
    """Whether pandas presents the cell as multi-valued — parquet returns a written list as
    ndarray."""
    return isinstance(value, (list, tuple, np.ndarray))


def is_missing_cell(value: Any) -> bool:
    """Catches `is_null_form`'s misses too: np.floating nan and bare np.datetime64 NaT."""
    if is_sequence_cell(value) or isinstance(value, dict):
        return False
    if is_null_form(value):
        return True
    if isinstance(value, np.floating):
        return bool(np.isnan(value))
    return bool(pd.api.types.is_scalar(value) and pd.isna(value))


def is_bool_cell(value: Any) -> bool:
    return isinstance(value, (bool, np.bool_))


def is_exact_int_cell(value: Any) -> bool:
    """Unlike `_is_int_cell`, never true for a whole-valued float — 2.0 stays a float."""
    return not is_bool_cell(value) and isinstance(value, (int, np.integer))


def is_exact_float_cell(value: Any) -> bool:
    """The float counterpart to `is_exact_int_cell`: true only for an actual float."""
    return isinstance(value, (float, np.floating))


# ── Cell-level type predicates ───────────────────────────────────────────────
# "May this cell sit in a column whose declared type is named T?" — one
# predicate per name in `CELL_TYPE_PREDICATES` below. The names are plain
# strings the caller supplies: the declared-type vocabulary is domain
# knowledge that lives in `app.models`, which `app.core` may not import (see
# pyproject.toml's "app.core does not import the domain models" contract), so
# a caller pins its own vocabulary against these keys instead.
#
# Deliberately permissive where pandas is lossy (numpy scalars, int-valued
# floats), deliberately strict where the distinction is real (a bool is not an
# int).


def _is_int_cell(value: Any) -> bool:
    # A Python bool is a subclass of int, but a column declared `int` that
    # holds True/False is a real mismatch — reject it explicitly.
    if is_bool_cell(value):
        return False
    if isinstance(value, (int, np.integer)):
        return True
    # An int column carrying a null is promoted to float64 by pandas, so a
    # whole-valued float is an int that survived a lossy round-trip, not a
    # type error. 1.5 in an int column still is one.
    return isinstance(value, (float, np.floating)) and float(value).is_integer()


def _is_float_cell(value: Any) -> bool:
    # Ints in a float column are fine (pandas will not preserve the
    # distinction anyway); bools are not.
    return isinstance(value, (float, np.floating)) or _is_int_cell(value)


def _is_str_cell(value: Any) -> bool:
    # np.str_ subclasses str; pandas `string` dtype yields plain str.
    return isinstance(value, str)


def _is_datetime_cell(value: Any) -> bool:
    # datetime.datetime covers pd.Timestamp (a subclass of it).
    return isinstance(value, (_dt.datetime, np.datetime64))


def _is_date_cell(value: Any) -> bool:
    # datetime.date covers datetime.datetime and pd.Timestamp: pandas has no
    # date-only dtype, so a `date` column round-trips as a Timestamp.
    return isinstance(value, (_dt.date, np.datetime64))


CELL_TYPE_PREDICATES: Mapping[str, Callable[[Any], bool]] = {
    "str": _is_str_cell,
    "int": _is_int_cell,
    "float": _is_float_cell,
    "bool": is_bool_cell,
    "datetime": _is_datetime_cell,
    "date": _is_date_cell,
}


def dtype_proves_cell_type(series: pd.Series, type_name: str) -> bool:
    """Whether the dtype alone proves every cell matches `type_name`; object dtype proves
    nothing."""
    dtype = series.dtype
    types = pd.api.types
    if types.is_object_dtype(dtype):
        return False  # the interesting case: cells must be inspected
    if type_name == "bool":
        return bool(types.is_bool_dtype(dtype))
    if type_name == "int":
        # A float dtype may be an int column that met a null — not provable
        # from the dtype, so fall through to the per-cell predicate, which
        # accepts whole-valued floats (see _is_int_cell).
        return bool(types.is_integer_dtype(dtype) and not types.is_bool_dtype(dtype))
    if type_name == "float":
        return bool(
            (types.is_float_dtype(dtype) or types.is_integer_dtype(dtype))
            and not types.is_bool_dtype(dtype)
        )
    if type_name == "str":
        return isinstance(dtype, pd.StringDtype)
    if type_name in ("datetime", "date"):
        return bool(types.is_datetime64_any_dtype(dtype))
    return False


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
    """compute_short_hash over a JSON dump of a WHOLE frame: its column
    labels in their own order, then its cells row by row in their own order,
    each cell collapsed through `collapse_null_forms` exactly as a row cell is.

    Column and row ORDER are part of the identity here, unlike a row
    fingerprint, where key order is deliberately irrelevant: a whole-frame
    transform may index positionally or depend on sort order, so a reordered
    input is a genuinely different input and must not resolve to the same
    cached output. The frame's index is not part of the identity — it does not
    survive the parquet round trip the payload takes."""
    payload = {
        "columns": [str(label) for label in frame.columns],
        "rows": [
            [collapse_null_forms(cell) for cell in row]
            for row in frame.itertuples(index=False, name=None)
        ],
    }
    return compute_short_hash(json.dumps(payload, separators=(",", ":"), default=str))


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
        write_frame_file(frame, path)

    def load_frame(self, collection: str, id: str) -> pd.DataFrame | None:
        path = self._path(collection, id)
        if not path.exists():
            return None
        return read_frame_file(path)

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
