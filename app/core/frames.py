"""Frames are addressed by (collection, id) like the document store, but stored one
parquet file per frame under a root directory. Also owns the value-level pandas
knowledge the stage cache and the schema checks key under - null forms, numpy
scalars, extension dtypes, what a cell's Python type says about it, frame identity."""
from __future__ import annotations

import csv as _csv
import datetime as _dt
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, cast

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.lib as pa_lib
import pyarrow.csv as csv
import pyarrow.parquet as pq

from app.core.errors import (
    CellIsNotAScalar,
    ColumnNotInFrame,
    FrameConcatMismatchError,
    RowOutOfRange,
)
from app.core.ids import validate_id
from app.core.json_types import JsonScalar
from app.core.ids import ID

# The on-disk extension for a frame file, named so every reader that
# distinguishes a parquet output from a csv one (by `Path.suffix`) compares
# against the same value instead of re-typing the literal.
PARQUET_SUFFIX = ".parquet"


# ── Frame files THIS codebase wrote ──────────────────────────────────────────
# Stage outputs, the frame cache, lineage sidecars, stage decisions, the review
# queue, eval results. The read is the exact inverse of the write: no schema, no
# coercion, no dtype argument reaches these. A frame that needs coercion on the
# way back in is a frame something already got wrong on the way out — see the
# `read_source_*` readers below, for formats that hold no types to read.


# The arrow-native pair. Parquet IS arrow, so these are the identity round trip:
# an int column that met a null comes back int64, where the pandas pair below
# returns it as float64 and loses which it was. The runtime reads and writes
# through these; the pandas pair exists for presentation, which formats cells as
# text and cannot use a table.
def read_frame_table(path: Path) -> pa.Table:
    if path.suffix == PARQUET_SUFFIX:
        return pq.read_table(path, use_pandas_metadata=True)
    return csv.read_csv(path)


def write_frame_table(table: pa.Table, path: Path) -> None:
    if path.suffix == PARQUET_SUFFIX:
        pq.write_table(table, path)
    else:
        csv.write_csv(table, path)


def read_frame_file(path: Path) -> pd.DataFrame:
    return _read_frame_parquet(path) if path.suffix == PARQUET_SUFFIX else pd.read_csv(path)


def write_frame_file(frame: pd.DataFrame, path: Path) -> None:
    if path.suffix == PARQUET_SUFFIX:
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False)


class FrameWrite(NamedTuple):
    path: Path
    # None when the parquet write succeeded; otherwise the reason it did not,
    # for a caller that reports the degradation to a reviewer.
    parquet_error: str | None


def write_frame_table_with_csv_fallback(table: pa.Table, path: Path) -> FrameWrite:
    """A table arrow itself cannot write is the rescued case; a disk error still propagates."""
    try:
        pq.write_table(table, path)
    except (pa_lib.ArrowException, ValueError, TypeError) as exc:
        csv_path = path.with_suffix(".csv")
        csv.write_csv(table, csv_path)
        return FrameWrite(csv_path, str(exc))
    return FrameWrite(path, None)


def write_frame_file_with_csv_fallback(frame: pd.DataFrame, path: Path) -> FrameWrite:
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
    return frame.to_csv(index=False)


# ── SOURCE files in a format that carries no types ───────────────────────────
# csv, xlsx and json-lines hold characters, not types, so pandas will guess one
# per column unless told otherwise. That is what the `dtype` pin is for, and why
# these readers exist apart from `read_frame_file`. Which columns to pin is
# domain knowledge read off a declared schema, and the "app.core does not import
# the domain models" contract keeps that above this layer: the caller works out
# the pins and passes plain types down, so these hold the pandas call and nothing
# else.
#
# There is deliberately no parquet entry. Parquet carries its own types, so who
# wrote the file changes nothing about how to read it — `read_frame_file`.


def read_source_csv(
    path: Path, *, dtype: Any = None, delimiter: str | None = None,
) -> pd.DataFrame:
    try:
        return _read_source_csv_with_encoding(path, dtype, delimiter, "utf-8")
    except UnicodeDecodeError:
        return _read_source_csv_with_encoding(path, dtype, delimiter, "windows-1252")


def _read_source_csv_with_encoding(
    path: Path, dtype: Any, delimiter: str | None, encoding: str,
) -> pd.DataFrame:
    with path.open(encoding=encoding, errors="strict", newline="") as handle:
        sample = handle.read(65_536)
    separator = delimiter if delimiter is not None else _detect_csv_delimiter(path, sample)
    return pd.read_csv(
        path, dtype=dtype, sep=separator, encoding=encoding, encoding_errors="strict"
    )


def _detect_csv_delimiter(path: Path, sample: str) -> str:
    widths = {
        delimiter: _read_header_width(sample, delimiter)
        for delimiter in (",", "\t")
    }
    header_delimiters = [
        delimiter for delimiter, width in widths.items()
        if width is not None and width > 1
    ]
    if len(header_delimiters) == 1:
        return header_delimiters[0]
    if not header_delimiters:
        return ","
    try:
        return _csv.Sniffer().sniff(sample, delimiters=",\t").delimiter
    except _csv.Error as exc:
        raise ValueError(
            f"cannot distinguish comma-separated from tab-separated content in {path}"
        ) from exc


def _read_header_width(sample: str, delimiter: str) -> int | None:
    try:
        return len(next(_csv.reader(sample.splitlines(), delimiter=delimiter, strict=True)))
    except StopIteration:
        return 1
    except _csv.Error:
        return None


def read_source_json_lines(path: Path, *, dtype: Any = None) -> pd.DataFrame:
    # `dtype=False` infers nothing, keeping a JSON string "002" a string, not the int 2.
    return pd.read_json(path, lines=True, dtype=dtype)


def read_source_excel(
    path: Path, *, sheet_name: str | int, header_row: int, dtype: Any = None,
) -> pd.DataFrame:
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
# `compute_row_fingerprint` as `"['a' 'b']"` rather than as a JSON array —
# the same data under a different cache key. `types_mapper` fires only on the
# arrow LIST types, so every scalar column keeps the numpy-backed dtype and
# the exact cell type `pd.read_parquet` gives it, and only the list columns
# move. `use_pandas_metadata` matches what `pd.read_parquet` asks for.
def _read_frame_parquet(path: Path) -> pd.DataFrame:
    table = pq.read_table(path, use_pandas_metadata=True)
    return table.to_pandas(types_mapper=_map_list_type_to_arrow_dtype)


def _map_list_type_to_arrow_dtype(arrow_type: pa.DataType) -> pd.ArrowDtype | None:
    if (
        pa.types.is_list(arrow_type)
        or pa.types.is_large_list(arrow_type)
        or pa.types.is_fixed_size_list(arrow_type)
    ):
        return pd.ArrowDtype(arrow_type)
    return None


def read_frame_column_names(path: Path) -> list[str]:
    if path.suffix == PARQUET_SUFFIX:
        # Every writer here saves with index=False, so there is no index column to subtract.
        return [str(name) for name in pq.read_schema(path).names]
    return [str(name) for name in pd.read_csv(path, nrows=0).columns]


# Concatenation is permissive about two things and strict about everything else.
# It promotes an all-null column (arrow `null`) to the type its sibling carries,
# and joins an int column to a double one — the shape pandas leaves behind when a
# null upcast a column before it reached the wire. A genuine conflict (str vs
# int) raises, where `pd.concat` would have merged it into an untyped object
# column. Differing column NAMES raise too: permissive promotion would otherwise
# fill the missing column with a value nothing supplied.
def concat_tables(tables: Sequence[pa.Table]) -> pa.Table:
    if not tables:
        return pa.table({})
    _reject_mismatched_columns(tables)
    return pa.concat_tables(tables, promote_options="permissive")


def _reject_mismatched_columns(tables: Sequence[pa.Table]) -> None:
    """Column ORDER is not checked: arrow matches fields by name and keeps the first table's."""
    reference = set(tables[0].schema.names)
    for position, table in enumerate(tables[1:], start=1):
        names = set(table.schema.names)
        if names != reference:
            raise FrameConcatMismatchError(
                f"table {position} does not carry the same columns as table 0: "
                f"only in table 0 {sorted(reference - names)}, "
                f"only in table {position} {sorted(names - reference)}"
            )


# ── the arrow/pandas seam ────────────────────────────────────────────────────
# Arrow is the wire format: what a stage is handed, returns, and is stored,
# hashed and validated as. pandas is materialized only where authored code reads
# a frame, and coerced back on the way out. These four
# are that boundary, and the only place the two type systems meet.


# Through the same `types_mapper` a store read uses, so a list cell is a `list`.
def read_cell(table: pa.Table, column: str, row_ordinal: int) -> JsonScalar:
    """A date reads as ISO, a NaN as absent; the arrow type decides, not the python object."""
    values = _select_column(table, column)
    if not 0 <= row_ordinal < table.num_rows:
        raise RowOutOfRange(
            f"row {row_ordinal:,} of a {table.num_rows:,}-row frame"
        )
    if pa.types.is_nested(values.type):
        raise CellIsNotAScalar(
            f"'{column}' holds {values.type}, which no scalar reader carries"
        )
    return _as_json_scalar(column, values.type, values[row_ordinal].as_py())


def _select_column(table: pa.Table, column: str) -> pa.ChunkedArray:
    if column not in table.column_names:
        raise ColumnNotInFrame(
            f"'{column}' is not in the frame — it has {sorted(table.column_names)}"
        )
    return table.column(column)


def _as_json_scalar(column: str, arrow_type: pa.DataType, cell: Any) -> JsonScalar:
    if cell is None:
        return None
    if pa.types.is_date(arrow_type) or pa.types.is_timestamp(arrow_type):
        return cell.isoformat()
    if pa.types.is_floating(arrow_type) and math.isnan(cell):
        return None
    if isinstance(cell, (str, int, float, bool)):
        return cell
    raise CellIsNotAScalar(
        f"'{column}' holds {arrow_type}, which reads as {type(cell).__name__}"
    )


def table_to_frame(table: pa.Table) -> pd.DataFrame:
    """An arrow table as pandas."""
    return table.to_pandas(types_mapper=_map_list_type_to_arrow_dtype)


def frame_to_table(frame: pd.DataFrame) -> pa.Table:
    """The inverse. `attrs` is dropped: pandas tries to JSON-serialize it into arrow
    metadata."""
    untagged = frame.copy(deep=False)
    untagged.attrs = {}
    return pa.Table.from_pandas(untagged, preserve_index=False)


def list_table_rows(table: pa.Table) -> list[dict[str, Any]]:
    """One dict per row — the row driver's view, and arrow's `.as_py()` throughout."""
    return table.to_pylist()


# `like` supplies the schema for an EMPTY result, where the rows name no column
# and arrow would otherwise infer a 0x0 table.
def table_from_rows(rows: list[dict[str, Any]], like: pa.Table | None = None) -> pa.Table:
    """Rows back into a table."""
    if rows:
        return pa.Table.from_pylist(rows)
    return like.schema.empty_table() if like is not None else pa.table({})


def list_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(label): value for label, value in record.items()}
        for record in frame.to_dict("records")
    ]


def collapse_null_forms(value: object) -> object:
    """Not `pd.isna`: its stubs refuse a bare `object`, and an array cell gives an ambiguous truth."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def is_null_form(value: object) -> bool:
    """Not a pd.isna test: an np.float32 nan or bare np.datetime64 NaT reads as non-null here."""
    return collapse_null_forms(value) is None


def is_sequence_cell(value: object) -> bool:
    return isinstance(value, (list, tuple, np.ndarray))


def is_missing_cell(value: Any) -> bool:
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
    return not is_bool_cell(value) and isinstance(value, (int, np.integer))


def is_exact_float_cell(value: Any) -> bool:
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
    if is_bool_cell(value):
        return False
    if isinstance(value, (int, np.integer)):
        return True
    # An int column carrying a null is promoted to float64 by pandas, so a
    # whole-valued float is an int that survived a lossy round-trip, not a
    # type error. 1.5 in an int column still is one.
    return isinstance(value, (float, np.floating)) and float(value).is_integer()


def _is_float_cell(value: Any) -> bool:
    return isinstance(value, (float, np.floating)) or _is_int_cell(value)


def _is_str_cell(value: Any) -> bool:
    return isinstance(value, str)


def _is_datetime_cell(value: Any) -> bool:
    return isinstance(value, (_dt.datetime, np.datetime64))


def _is_date_cell(value: Any) -> bool:
    # pandas has no date-only dtype, so a `date` column round-trips as a Timestamp.
    return isinstance(value, (_dt.date, np.datetime64))


CELL_TYPE_PREDICATES: Mapping[str, Callable[[Any], bool]] = {
    "str": _is_str_cell,
    "int": _is_int_cell,
    "float": _is_float_cell,
    "bool": is_bool_cell,
    "datetime": _is_datetime_cell,
    "date": _is_date_cell,
}


# Stronger than a pandas dtype, which is the point: pandas parks a list, a dict
# and mixed junk alike in `object`, settling nothing, so the caller had to inspect
# every cell of exactly the columns that matter most. Arrow types a list column
# `list<...>`, so the common case is settled without a cell being read.
#
# Satisfied, not equal: a declared `float` is satisfied by an integer column and
# a declared `datetime` by a date one. Every cell meets the declared type in both.
def is_schema_type_satisfied_by_arrow_type(arrow_type: pa.DataType, type_name: str) -> bool:
    """True where every cell of `arrow_type` meets `type_name`, with no cell read."""
    types = pa.types
    if type_name == "bool":
        return types.is_boolean(arrow_type)
    if type_name == "int":
        # `double` is NOT proof: a column that met a null before it reached arrow
        # may be a float carrying whole values rather than a type error. The
        # per-cell predicate accepts that; this declines to prove it either way.
        return types.is_integer(arrow_type) and not types.is_boolean(arrow_type)
    if type_name == "float":
        return (
            types.is_floating(arrow_type) or types.is_integer(arrow_type)
        ) and not types.is_boolean(arrow_type)
    if type_name == "str":
        return types.is_string(arrow_type) or types.is_large_string(arrow_type)
    if type_name in ("datetime", "date"):
        return types.is_timestamp(arrow_type) or types.is_date(arrow_type)
    return False


def find_arrow_list_value_type(arrow_type: pa.DataType) -> pa.DataType | None:
    """The element type of an arrow list, or None when `arrow_type` is not a list."""
    if (
        pa.types.is_list(arrow_type)
        or pa.types.is_large_list(arrow_type)
        or pa.types.is_fixed_size_list(arrow_type)
    ):
        return arrow_type.value_type
    return None


def convert_cell_to_json_native(value: object) -> JsonScalar:
    if isinstance(value, np.generic):
        native = value.item()
        if native is None or isinstance(native, (bool, int, float, str)):
            return native
        return str(native)
    return str(value)


def convert_row_to_json_cells(row: dict[str, Any]) -> dict[str, JsonScalar]:
    return {name: convert_cell_to_json_value(value) for name, value in row.items()}


def convert_cell_to_json_value(value: object) -> JsonScalar:
    """A null stays null: a blank cell a reader reads as blank must not arrive as "None"."""
    cell = collapse_null_forms(value)
    if cell is None or isinstance(cell, (bool, int, float, str)):
        return cell
    if isinstance(cell, _dt.datetime):
        return _render_moment(cell)
    return convert_cell_to_json_native(cell)


def _render_moment(moment: _dt.datetime) -> str:
    # Midnight reads as a date, which is what astype(str) gives a whole column of them.
    if moment.time() == _dt.time():
        return moment.date().isoformat()
    return moment.isoformat(sep=" ")


class FrameStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, collection: str, id: ID) -> Path:
        validate_id(collection)
        validate_id(id)
        return self.root / collection / f"{id}.parquet"

    def load_table(self, collection: str, id: ID) -> pa.Table | None:
        path = self._path(collection, id)
        return read_frame_table(path) if path.exists() else None

    def save_table(
        self, collection: str, id: ID, table: pa.Table, *, overwrite: bool = True
    ) -> None:
        path = self._path(collection, id)
        if path.exists() and not overwrite:
            raise FileExistsError(f"frame already exists: {collection}/{id}")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_frame_table(table, path)

    def exists(self, collection: str, id: ID) -> bool:
        return self._path(collection, id).exists()

    def list_ids(self, collection: str, prefix: str = "") -> list[ID]:
        root = self.root / validate_id(collection)
        if not root.is_dir():
            return []
        stored = (path.relative_to(root).with_suffix("").as_posix()
                  for path in root.rglob(f"*{PARQUET_SUFFIX}"))
        return sorted(id for id in stored if id.startswith(prefix))

    def read_payload(self, collection: str, id: ID) -> bytes | None:
        path = self._path(collection, id)
        return path.read_bytes() if path.exists() else None

    def write_payload(self, collection: str, id: ID, payload: bytes) -> None:
        """Verbatim: a payload copied between stores must not be re-encoded on the way."""
        path = self._path(collection, id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def delete(self, collection: str, id: ID) -> None:
        self._path(collection, id).unlink(missing_ok=True)


_frame_store: FrameStore | None = None


def configure_frame_store(store: FrameStore) -> None:
    global _frame_store
    _frame_store = store


def get_frame_store() -> FrameStore:
    if _frame_store is None:
        raise RuntimeError("frame store not configured; call configure_frame_store() first")
    return _frame_store


def is_frame_store_configured() -> bool:
    return _frame_store is not None
