"""Read a golden table out of a published notebook's COMMITTED output.

The notebook is never executed: journalists commit the rendered table because it is what
their readers see, so the answer a newsroom stood behind is already in the .ipynb JSON.
Cells are kept as rendered text; coercion belongs to the comparison, not the golden."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evals.harness.case import GoldenRow, GoldenTable

# pandas renders a long frame with an elided row rather than refusing. A golden holding one
# is a PREFIX of the answer, and comparing against it would report missing keys the newsroom
# actually published — so it is refused rather than trimmed.
_ELISION_MARKERS = frozenset({"...", "…"})

# A cell ending in `.head(n)` renders COMPLETELY, so the elision check passes while the
# answer itself is a prefix the author asked for. Told apart by arity: fewer rows than the
# cap means the cap never bit, exactly the cap means it almost certainly did.
_ROW_CAPS = re.compile(r"\.(head|tail|sample)\s*\(\s*(\d+)\s*\)")

# How a missing value ARRIVES in rendered HTML. A genuine string cell holding one of these
# would be misread as absent, which is the lesser error: reading a rendered NaN as the text
# "NaN" makes every missing cell disagree with a build that correctly produced nothing.
_RENDERED_ABSENT = frozenset({"", "NaN", "nan", "NaT", "None", "<NA>"})

# Placeholder for the rendered index while the table is being read, before it is dropped.
_DROPPED_INDEX = "__index__"


class _Output(BaseModel):
    # `data` is mime type -> payload, and a payload's shape varies by mime: the genuine
    # foreign-JSON boundary. Only text/html is read, and only as str-or-list-of-str.
    model_config = ConfigDict(extra="ignore")
    data: dict[str, Any] = Field(default_factory=dict)


class _CodeCell(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source: list[str] = Field(default_factory=list)
    outputs: list[_Output] = Field(default_factory=list)


def extract_golden_table(
    notebook: Path, code_cell_index: int, index_column: str | None
) -> GoldenTable:
    """Raises unless that cell rendered exactly one complete, un-elided HTML table.

    `index_column` names the rendered pandas index; None DROPS it, which is right when the
    index is pandas' own positional integer and carries no meaning."""
    cell = _find_code_cell(notebook, code_cell_index)
    html = _find_rendered_table_html(cell, notebook, code_cell_index)
    header, body = _read_table(html)
    columns = [index_column or _DROPPED_INDEX, *header[1:]]
    _refuse_elided(columns, body, notebook, code_cell_index)
    _refuse_capped_by_the_author(cell, len(body), notebook, code_cell_index)
    rows = _to_rows(columns, body)
    if index_column is None:
        columns = columns[1:]
        rows = [{k: v for k, v in row.items() if k != _DROPPED_INDEX} for row in rows]
    return GoldenTable(columns=columns, rows=rows)


def _find_code_cell(notebook: Path, code_cell_index: int) -> _CodeCell:
    cells = [
        cell
        for cell in json.loads(notebook.read_text(encoding="utf-8"))["cells"]
        if cell["cell_type"] == "code"
    ]
    if not 0 <= code_cell_index < len(cells):
        raise ValueError(f"{notebook} has {len(cells)} code cells; no code cell {code_cell_index}")
    return _CodeCell.model_validate(cells[code_cell_index])


def _refuse_capped_by_the_author(
    cell: _CodeCell, rows: int, notebook: Path, index: int
) -> None:
    source = "".join(cell.source)
    for call, cap in _ROW_CAPS.findall(source):
        if rows == int(cap):
            raise ValueError(
                f"code cell {index} of {notebook} rendered exactly {rows} rows and its source "
                f"calls .{call}({cap}) — the table is a prefix the author asked for, not the "
                f"whole answer, so it cannot be a golden"
            )


def _find_rendered_table_html(cell: _CodeCell, notebook: Path, code_cell_index: int) -> str:
    tables = [
        _join_payload(output.data["text/html"])
        for output in cell.outputs
        if "text/html" in output.data
    ]
    if len(tables) != 1:
        raise ValueError(
            f"code cell {code_cell_index} of {notebook} carries {len(tables)} stored HTML "
            f"outputs, not 1 — a golden must be one unambiguous rendered table"
        )
    return tables[0]


def _join_payload(payload: object) -> str:
    """A notebook stores a text payload as a str or as a list of line strs."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        return "".join(str(line) for line in payload)
    raise ValueError(f"a text/html output holds {type(payload).__name__}, not text")


def _read_table(html: str) -> tuple[list[str], list[list[str]]]:
    """Header names (position 0 is the index) and the data rows, all as text."""
    reader = _TableReader()
    reader.feed(html)
    rows = reader.rows
    if not rows:
        raise ValueError("the stored HTML holds no table rows")
    header_rows = [cells for kinds, cells in rows if set(kinds) == {"th"}]
    body = [cells for kinds, cells in rows if set(kinds) != {"th"}]
    if not header_rows or not body:
        raise ValueError(
            f"expected header and body rows, got {len(header_rows)} header / {len(body)} body"
        )
    return _merge_header_rows(header_rows), _refuse_ragged(body, len(header_rows[0]))


def _merge_header_rows(header_rows: list[list[str]]) -> list[str]:
    """pandas splits a named index across two header rows; take the last non-empty per column."""
    width = max(len(cells) for cells in header_rows)
    merged = [""] * width
    for cells in header_rows:
        for position, text in enumerate(cells):
            if text:
                merged[position] = text
    return merged


def _refuse_ragged(body: list[list[str]], width: int) -> list[list[str]]:
    ragged = [cells for cells in body if len(cells) != width]
    if ragged:
        raise ValueError(
            f"{len(ragged)} row(s) do not hold {width} cells — first is {ragged[0]}"
        )
    return body


def _refuse_elided(
    columns: list[str], body: list[list[str]], notebook: Path, cell: int
) -> None:
    if any(text in _ELISION_MARKERS for text in columns) or any(
        text in _ELISION_MARKERS for cells in body for text in cells
    ):
        raise ValueError(
            f"code cell {cell} of {notebook} rendered an ELIDED table — pandas cut rows or "
            f"columns, so this output is a prefix of the answer and cannot be a golden"
        )


def _to_rows(columns: list[str], body: list[list[str]]) -> list[GoldenRow]:
    return [
        {
            name: (None if text in _RENDERED_ABSENT else text)
            for name, text in zip(columns, cells)
        }
        for cells in body
    ]


class _TableReader(HTMLParser):
    """Every `<tr>` as (cell tags, cell texts) — `th` vs `td` is how a header row is told."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[list[str], list[str]]] = []
        self._kinds: list[str] = []
        self._cells: list[str] = []
        self._open: str | None = None

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag == "tr":
            self._kinds, self._cells = [], []
        elif tag in ("th", "td"):
            self._open = tag
            self._kinds.append(tag)
            self._cells.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("th", "td"):
            self._open = None
        elif tag == "tr" and self._cells:
            self.rows.append((self._kinds, [text.strip() for text in self._cells]))
            self._kinds, self._cells = [], []

    def handle_data(self, data: str) -> None:
        if self._open is not None and self._cells:
            self._cells[-1] += data
