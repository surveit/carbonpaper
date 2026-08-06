from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.harness.golden import extract_golden_table


def _notebook(tmp_path: Path, source: str, table_html: str) -> Path:
    path = tmp_path / "n.ipynb"
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "source": [source],
                        "outputs": [{"data": {"text/html": [table_html]}}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


_TWO_ROWS = """
<table>
 <thead><tr><th>state</th><th>pay</th></tr></thead>
 <tbody>
  <tr><th>Napa</th><td>41940.45</td></tr>
  <tr><th>Colusa</th><td>NaN</td></tr>
 </tbody>
</table>"""


def test_a_rendered_nan_is_read_as_absent(tmp_path):
    """pandas renders a missing value as the text NaN; a build correctly produces nothing."""
    golden = extract_golden_table(_notebook(tmp_path, "frame", _TWO_ROWS), 0, "state")
    assert golden.rows == [
        {"state": "Napa", "pay": "41940.45"},
        {"state": "Colusa", "pay": None},
    ]


def test_an_elided_table_is_refused(tmp_path):
    elided = _TWO_ROWS.replace("<td>NaN</td>", "<td>...</td>")
    with pytest.raises(ValueError, match="ELIDED"):
        extract_golden_table(_notebook(tmp_path, "frame", elided), 0, "state")


def test_a_table_capped_by_the_author_is_refused(tmp_path):
    """Two rows rendered from a `.head(2)` is a prefix the author asked for."""
    with pytest.raises(ValueError, match=r"\.head\(2\)"):
        extract_golden_table(_notebook(tmp_path, "frame.head(2)", _TWO_ROWS), 0, "state")


def test_a_cap_the_data_never_reached_is_allowed(tmp_path):
    """Two rows under a `.head(60)` means the cap never bit — this is the whole answer."""
    golden = extract_golden_table(_notebook(tmp_path, "frame.head(60)", _TWO_ROWS), 0, "state")
    assert len(golden.rows) == 2


def test_a_cell_with_no_stored_table_is_refused(tmp_path):
    path = tmp_path / "n.ipynb"
    path.write_text(
        json.dumps({"cells": [{"cell_type": "code", "source": [], "outputs": []}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="0 stored HTML outputs"):
        extract_golden_table(path, 0, "state")
