from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pytest

from app.core.errors import CellIsNotAScalar, ColumnNotInFrame, RowOutOfRange
from app.core.frames import read_cell

# The firms behind the Venezuela LDA total, as that run aggregated them.
_BY_FIRM = pa.table({
    "registrant_org": ["BALLARD PARTNERS", "BROWNSTEIN HYATT FARBER SCHRECK, LLP",
                       "CHECKMATE GOVERNMENT RELATIONS"],
    "income_usd_total": [2530000.0, 510000.0, 250000.0],
})


def test_a_cell_keeps_its_type_rather_than_becoming_text():
    assert read_cell(_BY_FIRM, "income_usd_total", 0) == 2530000.0
    assert isinstance(read_cell(_BY_FIRM, "income_usd_total", 0), float)


def test_any_row_of_the_frame_is_readable():
    assert read_cell(_BY_FIRM, "registrant_org", 2) == "CHECKMATE GOVERNMENT RELATIONS"


def test_a_date_reads_as_iso():
    table = pa.table({"date_posted": pa.array([dt.date(2026, 6, 30)], type=pa.date32())})
    assert read_cell(table, "date_posted", 0) == "2026-06-30"


def test_a_timestamp_reads_as_iso():
    table = pa.table({"posted_at": pa.array([dt.datetime(2026, 6, 30, 14, 27)])})
    assert read_cell(table, "posted_at", 0) == "2026-06-30T14:27:00"


def test_a_null_reads_as_absent():
    table = pa.table({"capacity_tonnes_ffb_hour": pa.array([None], type=pa.float64())})
    assert read_cell(table, "capacity_tonnes_ffb_hour", 0) is None


def test_a_nan_reads_as_absent_rather_than_as_the_text_nan():
    table = pa.table({"capacity_tonnes_ffb_hour": [float("nan")]})
    assert read_cell(table, "capacity_tonnes_ffb_hour", 0) is None


def test_a_column_the_frame_does_not_have_is_refused_and_lists_what_it_has():
    with pytest.raises(ColumnNotInFrame, match="registrant_org"):
        read_cell(_BY_FIRM, "total_expenses_usd", 0)


def test_a_row_past_the_end_is_refused_and_says_how_many_there_are():
    with pytest.raises(RowOutOfRange, match="row 3 of a 3-row frame"):
        read_cell(_BY_FIRM, "income_usd_total", 3)


def test_a_negative_row_is_refused_rather_than_read_from_the_end():
    with pytest.raises(RowOutOfRange):
        read_cell(_BY_FIRM, "income_usd_total", -1)


def test_a_list_cell_is_refused_by_what_it_holds():
    table = pa.table({"issue_codes": [["TRD", "FOR"]]})
    with pytest.raises(CellIsNotAScalar, match="list<item: string>"):
        read_cell(table, "issue_codes", 0)
