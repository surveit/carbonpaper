"""Render a comparison as a standalone HTML page: the golden and the build side by side,
in the pairing the verdict was computed from.

Markup lives in Python here rather than a template because `evals/` has no template
engine; `tests/arch/test_no_html_in_python.py` governs `app/` only, where markup does
have somewhere else to live."""
from __future__ import annotations

from html import escape
from pathlib import Path

from evals.harness.case import Case, CellValue
from evals.harness.compare import AlignedPair, Comparison

_STYLE = """
:root { --ink:#16181d; --soft:#5b6270; --rule:#dfe3ea; --bg:#fbfbfc; --panel:#fff;
  --gone:#b4232a; --gone-bg:#fdf2f2; --new:#1c6b45; --new-bg:#f0f8f3;
  --diff:#8a5a00; --diff-bg:#fff8e6; --code:#f4f5f8; }
@media (prefers-color-scheme: dark) { :root {
  --ink:#e6e8ec; --soft:#9aa3b2; --rule:#2b3038; --bg:#14161a; --panel:#1b1e24;
  --gone:#ff8085; --gone-bg:#2a1a1c; --new:#6fd39d; --new-bg:#16241d;
  --diff:#f0c368; --diff-bg:#2b2416; --code:#21252c; } }
* { box-sizing:border-box; }
body { margin:0; padding:2.5rem 1.5rem 5rem; background:var(--bg); color:var(--ink);
  font:15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
main { max-width:78rem; margin:0 auto; }
h1 { font-size:1.5rem; margin:0 0 .3rem; letter-spacing:-.02em; }
.sub { color:var(--soft); margin:0 0 1.8rem; }
.tally { display:flex; flex-wrap:wrap; gap:1.6rem; padding:.9rem 1.1rem; margin:0 0 1.6rem;
  background:var(--panel); border:1px solid var(--rule); border-radius:6px; }
.tally div { font-size:.8rem; color:var(--soft); text-transform:uppercase;
  letter-spacing:.05em; }
.tally strong { display:block; font-size:1.35rem; color:var(--ink); letter-spacing:-.02em;
  font-variant-numeric:tabular-nums; }
.scroll { overflow-x:auto; border:1px solid var(--rule); border-radius:6px;
  background:var(--panel); }
table { border-collapse:collapse; width:100%; font-size:.85rem; }
th, td { padding:.4rem .6rem; border-bottom:1px solid var(--rule); text-align:left;
  white-space:nowrap; }
thead th { position:sticky; top:0; background:var(--panel); font-size:.7rem; color:var(--soft);
  text-transform:uppercase; letter-spacing:.05em; z-index:1; }
thead th.side { border-bottom:2px solid var(--rule); }
td.pos { color:var(--soft); font-variant-numeric:tabular-nums; text-align:right; }
td.rail { border-left:2px solid var(--rule); }
tr.missing td { background:var(--gone-bg); }
tr.extra td { background:var(--new-bg); }
tr.changed td { background:var(--diff-bg); }
td.cell-diff { color:var(--diff); font-weight:600; }
.tag { display:inline-block; font-size:.65rem; font-weight:700; text-transform:uppercase;
  letter-spacing:.07em; padding:.1em .45em; border-radius:3px; border:1px solid currentColor; }
.tag.missing { color:var(--gone); }
.tag.extra { color:var(--new); }
.tag.changed { color:var(--diff); }
.legend { margin:1.2rem 0 0; color:var(--soft); font-size:.85rem; }
code { background:var(--code); padding:.1em .35em; border-radius:3px; font-size:.85em;
  font-family:ui-monospace, SFMono-Regular, Menlo, monospace; }
"""


def write_comparison_html(
    path: Path, case: Case, comparison: Comparison, pairs: list[AlignedPair]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(case, comparison, pairs), encoding="utf-8")


def _render(case: Case, comparison: Comparison, pairs: list[AlignedPair]) -> str:
    columns = case.golden.columns
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{escape(case.case_id)} · expected vs built</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n<main>\n"
        f"<h1>{escape(case.case_id)}</h1>\n"
        f"<p class=\"sub\">Published answer on the left, carbonpaper build on the right, "
        f"paired positionally. Source: <code>{escape(case.source.path)}</code> at "
        f"<code>{escape(case.source.commit[:10])}</code>.</p>\n"
        f"{_tally(comparison)}\n"
        f"<div class=\"scroll\"><table>\n{_head(columns)}\n<tbody>\n"
        + "\n".join(_row(pair, columns) for pair in pairs)
        + "\n</tbody></table></div>\n"
        f"{_legend(case)}\n</main>\n</body>\n</html>\n"
    )


def _tally(comparison: Comparison) -> str:
    counts = [
        ("golden rows", comparison.golden_rows),
        ("build rows", comparison.output_rows),
        ("aligned in order", comparison.aligned_rows),
        ("row differences", len(comparison.row_differences)),
        ("cell differences", len(comparison.cell_differences)),
    ]
    return (
        "<div class=\"tally\">"
        + "".join(f"<div>{escape(name)}<strong>{value}</strong></div>" for name, value in counts)
        + "</div>"
    )


def _head(columns: list[str]) -> str:
    heads = "".join(f"<th>{escape(name)}</th>" for name in columns)
    rail = "".join(
        f"<th class='{'side' if i == 0 else ''}'>{escape(name)}</th>"
        for i, name in enumerate(columns)
    )
    return (
        "<thead><tr><th></th><th>#</th>" + heads + "<th class='side'></th>" + rail
        + "</tr></thead>"
    )


def _row(pair: AlignedPair, columns: list[str]) -> str:
    kind = (
        "missing" if pair.actual is None
        else "extra" if pair.golden is None
        else "changed" if pair.differing_columns
        else ""
    )
    tag = f"<span class='tag {kind}'>{kind}</span>" if kind else ""
    position = pair.golden_position if pair.golden_position is not None else pair.output_position
    left = "".join(
        _cell(pair.golden, name, name in pair.differing_columns) for name in columns
    )
    right = "".join(
        _cell(pair.actual, name, name in pair.differing_columns, rail=(index == 0))
        for index, name in enumerate(columns)
    )
    return (
        f"<tr class='{kind}'><td>{tag}</td><td class='pos'>{position}</td>"
        f"{left}<td class='rail'></td>{right}</tr>"
    )


def _cell(
    row: dict[str, CellValue] | None, column: str, differs: bool, rail: bool = False
) -> str:
    classes = " ".join(filter(None, ["cell-diff" if differs else "", "rail" if rail else ""]))
    if row is None:
        return f"<td class='{classes}'></td>"
    value = row.get(column)
    return f"<td class='{classes}'>{'' if value is None else escape(str(value))}</td>"


def _legend(case: Case) -> str:
    return (
        "<p class=\"legend\"><span class='tag missing'>missing</span> in the published "
        "answer, absent from the build &nbsp;·&nbsp; <span class='tag extra'>extra</span> "
        "in the build, absent from the published answer &nbsp;·&nbsp; "
        "<span class='tag changed'>changed</span> both tables have this position and a "
        f"column disagrees. Numbers compared at a relative tolerance of {case.tolerance:g}; "
        "row order is part of the answer, so the sort the brief states is checked too.</p>"
    )
