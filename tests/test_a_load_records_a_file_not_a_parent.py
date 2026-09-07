"""A load's file record decodes off a sidecar written either side of the split."""
from __future__ import annotations

from pathlib import Path, PurePath

import pandas as pd
import pyarrow as pa

from app.runtime.lineage import (
    LINEAGE_SCHEMA,
    TRACE_EDGE_KIND_KEY,
    TRACE_SOURCE_COLUMNS_KEY,
    TRACE_SOURCE_FILE_KEY,
    TRACE_SOURCE_ROW_KEY,
    TRACE_SOURCE_SHA_KEY,
    TRACE_SOURCE_STAGE_KEY,
    RowLineage,
)
from app.models import Stage, parse_stage
from app.runtime.stages.input_data import read_input_data
from conftest import make_run_context, place_stage

_COLUMNS = [{"name": "grant_id", "type": "str", "nullable": True}]


def _stage(paths: list[str]) -> Stage:
    return parse_stage({
        "id": "both_files", "description": "load", "type": "input_data",
        "connector": {"kind": "file", "params": {"paths": paths, "format": "csv"}},
        "signature": {"form": "replaces", "produces": _COLUMNS},
    })


def _two_files(tmp_path: Path) -> list[str]:
    east = pd.DataFrame({"grant_id": ["G-001", "G-002", "G-003"]})
    west = pd.DataFrame({"grant_id": ["G-004", "G-005"]})
    east.to_csv(tmp_path / "east.csv", index=False)
    west.to_csv(tmp_path / "west.csv", index=False)
    return [str(tmp_path / "east.csv"), str(tmp_path / "west.csv")]


def _load(tmp_path: Path) -> RowLineage:
    output = read_input_data(place_stage(_stage(_two_files(tmp_path))),
                             ctx=make_run_context())
    assert output.lineage is not None
    return output.lineage


def encode_a_pre_split_sidecar(stage_id: str, lineage: RowLineage) -> pa.Table:
    # How a load wrote its file record before the split: an edge pointing at itself.
    read = [lineage.source(row) for row in range(len(lineage))]
    assert all(one is not None for one in read)
    return pa.table({
        TRACE_SOURCE_STAGE_KEY: [[stage_id] for _ in read],
        TRACE_SOURCE_ROW_KEY: [[one.row_ordinal] for one in read if one],
        TRACE_EDGE_KIND_KEY: [["direct"] for _ in read],
        TRACE_SOURCE_COLUMNS_KEY: [[[]] for _ in read],
        TRACE_SOURCE_FILE_KEY: [[one.file] for one in read if one],
        TRACE_SOURCE_SHA_KEY: [[one.file_sha or ""] for one in read if one],
    }, schema=LINEAGE_SCHEMA)


def test_a_pre_split_self_edge_decodes_as_the_file_the_row_was_read_from(tmp_path):
    lineage = _load(tmp_path)
    read_back = RowLineage.from_table(encode_a_pre_split_sidecar("both_files", lineage))

    # The stage itself was never a step upstream, so nothing decodes as a parent.
    assert list(read_back.parents) == [[] for _ in range(len(lineage))]
    read = [read_back.source(row) for row in range(len(read_back))]
    assert [PurePath(one.file).name for one in read if one] == [
        "east.csv", "east.csv", "east.csv", "west.csv", "west.csv"]
    # Row 3 of the frame is row 0 of the second file — the number the bug read as a frame row.
    assert [one.row_ordinal for one in read if one] == [0, 1, 2, 0, 1]


def test_a_row_count_per_file_matches_the_files_on_disk(tmp_path):
    paths = _two_files(tmp_path)
    lineage = RowLineage.from_table(encode_a_pre_split_sidecar("both_files", _load(tmp_path)))
    per_file = {PurePath(path).name: len(pd.read_csv(path)) for path in paths}

    read_from = [lineage.source(row) for row in range(len(lineage))]
    counted = {name: sum(1 for one in read_from if one and PurePath(one.file).name == name)
               for name in per_file}
    assert counted == per_file


def test_both_encodings_of_one_load_decode_the_same(tmp_path):
    lineage = _load(tmp_path)
    assert (RowLineage.from_table(encode_a_pre_split_sidecar("both_files", lineage))
            == RowLineage.from_table(lineage.to_table()) == lineage)
