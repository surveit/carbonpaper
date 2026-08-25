"""Architecture: a page prints the row number it was served; no script works one out."""
from __future__ import annotations

import re
from pathlib import Path

from arch.scope import scan_all_text

_SCRIPTS = (".js", ".html")
# `ordinal + 1`, `r.ordinal+1`, `ev.row + 1` — a row counted client-side.
_COUNTS_A_ROW = re.compile(r"[\w$.\[\]]*(?:row|ordinal)[\w$.\[\]]*\s*\+\s*1\b", re.IGNORECASE)
# The scripts the rule exists for: a rename fails here rather than guarding nothing.
_GUARDED = ("run_log.js", "scope_map.js", "lineage.html")


def find_scripts_counting_rows(paths: list[Path]) -> list[str]:
    offenders = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _COUNTS_A_ROW.search(line):
                offenders.append(f"{path.name}:{lineno}  {line.strip()}")
    return offenders


def test_no_script_counts_a_row_number() -> None:
    offenders = find_scripts_counting_rows(scan_all_text(_SCRIPTS))
    assert not offenders, (
        "A run files its rows from 0 and every link, citation and record carries that "
        "ordinal; the number a reader sees is one higher, and render_row_number "
        "(app/web/config.py) is the only place that step is taken. Templates reach it "
        "through the `row_number` filter, and a script is handed a finished number in "
        "its payload — `row_number` on a trace node, `number` on a drawn row, "
        "`row_label` on a log event. A script that counts for itself is how a page and "
        "the link beside it come to name one row two things:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_reaches_the_scripts_it_guards() -> None:
    scanned = {path.name for path in scan_all_text(_SCRIPTS)}
    missing = [name for name in _GUARDED if name not in scanned]
    assert not missing, (
        f"the scan no longer reaches {missing} — these are the scripts that print row "
        "numbers, so a rule that cannot see them guards nothing"
    )


def test_the_pattern_tells_a_count_from_the_rest() -> None:
    for counted in ("var n = ordinal + 1;", "r.ordinal+1", "rows[0] + 1", "ev.row + 1"):
        assert _COUNTS_A_ROW.search(counted), counted
    assert _COUNTS_A_ROW.search("row ${n.row_number}") is None
    # Un-numbering is the other direction: what the reader typed, back to an ordinal.
    assert _COUNTS_A_ROW.search("lineageHref(COORD.stage_id, number - 1)") is None
    assert _COUNTS_A_ROW.search('"step": i + 1') is None
