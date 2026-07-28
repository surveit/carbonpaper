"""_read_xlsx maps connector params onto pd.read_excel (app/runtime/stages/input_data.py)."""
from __future__ import annotations

from pathlib import Path

import openpyxl

from app.models.stage import FileFormat
from app.runtime.stages.input_data import _read_xlsx


def _write(tmp_path: Path, rows: list[list[object]], sheets: dict[str, list[list[object]]] | None = None) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Sheet1"
    for row in rows:
        ws.append(row)
    for name, extra in (sheets or {}).items():
        other = wb.create_sheet(name)
        for row in extra:
            other.append(row)
    path = tmp_path / "book.xlsx"
    wb.save(path)
    return path


def test_xlsx_is_a_declarable_format():
    assert FileFormat("xlsx") is FileFormat.xlsx


def test_reads_first_sheet_with_header_on_row_one(tmp_path):
    path = _write(tmp_path, [["client", "income"], ["ACME", 1000], ["BETA", 2000]])
    df = _read_xlsx(path, {})
    assert list(df.columns) == ["client", "income"]
    assert len(df) == 2
    assert df.iloc[1]["client"] == "BETA"
    assert df.iloc[0]["income"] == 1000
