"""An exported bundle is handed to editors, lawyers and sources. Its trace pages
must identify the input they came from without carrying the author's filesystem.
Both halves are load-bearing: a page that disclosed nothing would pass the first
assertion and defeat the artifact's purpose, so the second one guards it."""
from __future__ import annotations

import hashlib
from pathlib import Path

from test_export_row_trace import SCORE_ID, at, exporter  # noqa: F401  (fixture)


def _render_page(exporter) -> str:  # noqa: F811
    from_file = exporter.output_dir / "index.html"
    href = exporter.export_row_trace(SCORE_ID, from_file, row=at(0))
    return (from_file.parent / href).resolve().read_text(encoding="utf-8")


def _input_file(exporter) -> Path:  # noqa: F811
    """The csv the fixture's load stage reads — runs/<id> sits two below the project."""
    return exporter.run_dir.parents[1] / "rows.csv"


def test_the_exported_page_carries_no_absolute_filesystem_path(exporter, tmp_path):  # noqa: F811
    html = _render_page(exporter)
    root = str(tmp_path)
    assert root not in html, f"the page discloses the project root {root}"
    assert root.replace("\\", "/") not in html
    assert _input_file(exporter).parent.name not in html  # nor the directory above the file


def test_the_exported_page_still_identifies_the_input_file(exporter):  # noqa: F811
    """Not merely non-empty: the page names the file and pins the exact bytes,
    which is what an outside reader needs to re-run the claim."""
    html = _render_page(exporter)
    data = _input_file(exporter)
    assert data.name in html
    assert hashlib.sha256(data.read_bytes()).hexdigest() in html
