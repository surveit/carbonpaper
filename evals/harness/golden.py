"""Read a golden table out of a published notebook's COMMITTED output.

The notebook is never executed: journalists commit the rendered table because it is what
their readers see, so the answer a newsroom stood behind is already in the .ipynb JSON.
Cells are kept as rendered text; coercion belongs to the comparison, not the golden."""
from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from evals.harness.case import GoldenRow, GoldenTable

# pandas renders a long frame with an elided row rather than refusing. A golden holding one
# is a PREFIX of the answer, and comparing against it would report missing keys the newsroom
# actually published — so it is refused rather than trimmed.
_ELISION_MARKERS = frozenset({"...", "…"})


def extract_golden_table(notebook: Path, code_cell_index: int, key_column: str) -> GoldenTable:
    """Raises unless that cell rendered exactly one complete, un-elided HTML table."""
    html = _find_rendered_table_html(notebook, code_cell_index)
    header, body = _read_table(html)
    columns = [key_column, *header[1:]]
    _refuse_elided(columns, body, notebook, code_cell_index)
    return GoldenTable(key_column=key_column, columns=columns, rows=_to_rows(columns, body))


def _find_rendered_table_html(notebook: Path, code_cell_index: int) -> str:
    cells = [
        cell
        for cell in json.loads(notebook.read_text(encoding="utf-8"))["cells"]
        if cell["cell_type"] == "code"
    ]
    if not 0 <= code_cell_index < len(cells):
        raise ValueError(f"{notebook} has {len(cells)} code cells; no code cell {code_cell_index}")
    tables = [
        "".join(output["data"]["text/html"])
        for output in cells[code_cell_index].get("outputs", [])
        if "text/html" in output.get("data", {})
    ]
    if len(tables) != 1:
        raise ValueError(
            f"code cell {code_cell_index} of {notebook} carries {len(tables)} stored HTML "
            f"outputs, not 1 — a golden must be one unambiguous rendered table"
        )
    return tables[0]


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
        {name: (text if text != "" else None) for name, text in zip(columns, cells)}
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
