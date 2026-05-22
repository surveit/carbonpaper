"""
Render drift story cards (HTML) and a scoreboard-style index.

The index is the front door: sortable, filterable, scores visible at a glance.
Individual story cards are linked from index rows.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


PAGE_CSS = """
body { font-family: -apple-system, "Segoe UI", Arial, sans-serif; max-width: 920px; margin: 24px auto; padding: 0 24px; color: #1a1a1a; line-height: 1.5; }
h1 { margin-top: 0; }
.score-pill { display: inline-block; padding: 4px 12px; border-radius: 14px; font-weight: 600; font-size: 14px; }
.score-9, .score-10 { background: #fde8e8; color: #802020; }
.score-7, .score-8 { background: #fff4e6; color: #7a4a00; }
.score-5, .score-6 { background: #fffbe6; color: #5a4a00; }
.score-3, .score-4 { background: #f0f4fa; color: #1a3a72; }
.score-0, .score-1, .score-2 { background: #f0f0f0; color: #666; }
.party-D { color: #1f4a8a; } .party-R { color: #8a1f1f; } .party-I { color: #4a4a4a; }
.meta { color: #666; font-size: 14px; margin-bottom: 14px; }
.hypothesis { background: #f7f7f4; border-left: 4px solid #2a6ac8; padding: 12px 16px; border-radius: 4px; margin: 14px 0; font-size: 15px; }
section { background: #fff; border: 1px solid #ddd; border-radius: 6px; padding: 14px 18px; margin: 14px 0; }
section h2 { margin: 0 0 8px; font-size: 16px; }
.drift-item { padding: 8px 0; border-bottom: 1px dashed #e0e0e0; font-size: 14px; }
.drift-item:last-child { border-bottom: none; }
.direction { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; margin-right: 6px; }
.dir-added { background: #e8f8e8; color: #1f5a1f; }
.dir-dropped { background: #fde8e8; color: #802020; }
.dir-intensified { background: #fff4e6; color: #7a4a00; }
.dir-diminished { background: #f0f0f0; color: #666; }
blockquote { margin: 6px 0; padding: 6px 12px; border-left: 2px solid #ccc; font-style: italic; color: #444; font-size: 13px; }
.q-label { font-weight: 600; font-size: 12px; color: #666; }
.phrases { font-size: 13px; }
.phrases code { background: #f0f0f0; padding: 1px 6px; border-radius: 3px; margin: 0 2px; }
.disclaimer { background: #fffbe6; border: 1px solid #d4c060; padding: 10px 14px; border-radius: 4px; margin: 14px 0; font-size: 13px; color: #5a4a00; }
footer { margin-top: 32px; color: #888; font-size: 12px; border-top: 1px solid #ddd; padding-top: 12px; }
"""

INDEX_CSS = """
body { font-family: -apple-system, "Segoe UI", Arial, sans-serif; max-width: 1100px; margin: 24px auto; padding: 0 24px; color: #1a1a1a; }
h1 { margin-top: 0; }
.controls { margin: 16px 0; font-size: 14px; }
.controls input, .controls select { padding: 4px 8px; font-size: 14px; border: 1px solid #ccc; border-radius: 3px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { padding: 8px 10px; border-bottom: 1px solid #eee; text-align: left; vertical-align: top; }
th { background: #f0f0ed; cursor: pointer; user-select: none; }
th:hover { background: #e0e0dd; }
tr.party-D { background-color: #f7faff; }
tr.party-R { background-color: #fff8f7; }
tr:hover { background-color: #fffbe0; }
.score-cell { font-weight: 600; text-align: center; }
.score-9, .score-10 { color: #802020; }
.score-7, .score-8 { color: #7a4a00; }
.score-5, .score-6 { color: #5a4a00; }
.score-3, .score-4 { color: #1a3a72; }
.score-0, .score-1, .score-2 { color: #999; }
.headline { color: #333; }
.meta { color: #666; font-size: 13px; }
a { color: #1a3a72; text-decoration: none; }
a:hover { text-decoration: underline; }
"""


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _coerce(v):
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, (list, dict)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    # numpy arrays (parquet round-trip can hand us these for json-typed cols)
    try:
        return list(v)
    except TypeError:
        return v


def _int_or_zero(v) -> int:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _str_or_default(v, default: str) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return default
    return str(v)


def _render_card(row: pd.Series, output_dir: Path) -> tuple[Path, int]:
    name = row["name"]
    party = row.get("party", "")
    state = row.get("state", "")
    chamber = row.get("chamber", "")
    score = _int_or_zero(row.get("notability_score"))
    headline = _str_or_default(row.get("headline"), "no drift detected")
    hypothesis = _str_or_default(row.get("story_hypothesis"), "(no hypothesis emitted)")

    topical = _coerce(row.get("topical_drift")) or []
    stance = _coerce(row.get("stance_drift")) or []
    new_phrases = _coerce(row.get("new_talking_points")) or []
    abandoned = _coerce(row.get("abandoned_talking_points")) or []
    early_urls = _coerce(row.get("early_url_map")) or {}
    recent_urls = _coerce(row.get("recent_url_map")) or {}

    topical_html = ""
    for t in topical:
        if not isinstance(t, dict):
            continue
        d = t.get("direction", "")
        topical_html += (
            f'<div class="drift-item"><span class="direction dir-{_esc(d)}">{_esc(d)}</span>'
            f'<strong>{_esc(t.get("topic", ""))}</strong> — {_esc(t.get("explanation", ""))}</div>'
        )

    stance_html = ""
    for s in stance:
        if not isinstance(s, dict):
            continue
        stance_html += (
            f'<div class="drift-item">'
            f'<strong>{_esc(s.get("topic", ""))}</strong> — {_esc(s.get("explanation", ""))}'
            f'<div class="q-label">EARLY</div><blockquote>{_esc(s.get("early_quote", ""))}</blockquote>'
            f'<div class="q-label">RECENT</div><blockquote>{_esc(s.get("recent_quote", ""))}</blockquote>'
            f'</div>'
        )

    phrases_html = ""
    if new_phrases:
        phrases_html += '<div class="phrases"><strong>New phrases:</strong> ' + " ".join(
            f"<code>{_esc(p)}</code>" for p in new_phrases
        ) + "</div>"
    if abandoned:
        phrases_html += '<div class="phrases"><strong>Abandoned phrases:</strong> ' + " ".join(
            f"<code>{_esc(p)}</code>" for p in abandoned
        ) + "</div>"

    bid = row["entity_id"].split(":", 1)[-1]
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{_esc(name)} — drift report</title>
<style>{PAGE_CSS}</style></head><body>
<a href="index.html">← all stories</a>
<h1 class="party-{_esc(party[:1] or 'I')}">{_esc(name)}</h1>
<div class="meta">{_esc(party)} · {_esc(state)} · {_esc(chamber)} · entity <code>{_esc(row['entity_id'])}</code></div>
<div><span class="score-pill score-{score}">notability {score}/10</span> &nbsp; {_esc(headline)}</div>
<div class="disclaimer">
  This card is an LLM-authored hypothesis for journalists to investigate, not a finding.
  All quotes are extracted from press releases — verify against the cited URL before publication.
</div>
<div class="hypothesis"><strong>Story hypothesis:</strong> {_esc(hypothesis)}</div>
{f'<section><h2>Topical drift</h2>{topical_html}</section>' if topical_html else ''}
{f'<section><h2>Stance drift</h2>{stance_html}</section>' if stance_html else ''}
{f'<section><h2>Talking points</h2>{phrases_html}</section>' if phrases_html else ''}
<footer>Generated {datetime.now():%Y-%m-%d %H:%M} · Drift v1 prototype</footer>
</body></html>"""

    path = output_dir / f"{bid}.html"
    path.write_text(page, encoding="utf-8")
    return path, len(page.encode("utf-8"))


def _render_index(rows: list[dict], output_dir: Path, total_members: int) -> None:
    rows_sorted = sorted(rows, key=lambda r: (-int(r["score"] or 0), r["name"]))
    table_rows = "\n".join(
        f'<tr class="party-{_esc(r["party"][:1] or "I")}">'
        f'<td class="score-cell score-{int(r["score"] or 0)}">{r["score"]}</td>'
        f'<td><a href="{_esc(r["filename"])}">{_esc(r["name"])}</a></td>'
        f'<td>{_esc(r["party"])}</td><td>{_esc(r["state"])}</td><td>{_esc(r["chamber"])}</td>'
        f'<td class="headline">{_esc(r["headline"])}</td>'
        f'<td class="meta">{r["stance_count"]}/{r["topical_count"]}</td>'
        f'</tr>'
        for r in rows_sorted
    )
    index_html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Drift stories</title>
<style>{INDEX_CSS}</style></head><body>
<h1>Drift stories</h1>
<p class="meta">{len(rows_sorted)} candidate stories from {total_members} compared members. Generated {datetime.now():%Y-%m-%d}.</p>
<p class="meta">Sort by clicking column headers (TODO). Columns: notability/name/party/state/chamber/headline/stance:topical counts.</p>
<table id="t"><thead><tr>
<th>Score</th><th>Member</th><th>Party</th><th>State</th><th>Chamber</th>
<th>Headline</th><th>S/T</th>
</tr></thead><tbody>
{table_rows}
</tbody></table></body></html>"""
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")


def transform(
    drift_brief: pd.DataFrame,
    notability_rank: pd.DataFrame,
    output_dir: Path | None = None,
) -> pd.DataFrame:
    if output_dir is None:
        output_dir = Path("build/drift_stories")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rank_by_id = notability_rank.set_index("entity_id")
    out_rows = []
    index_rows = []

    for _, row in drift_brief.iterrows():
        path, size = _render_card(row, output_dir)
        out_rows.append({
            "entity_id": row["entity_id"],
            "output_path": str(path),
            "rendered_at": datetime.now().isoformat(timespec="seconds"),
            "bytes": size,
        })
        eid = row["entity_id"]
        bid = eid.split(":", 1)[-1]
        index_rows.append({
            "name": row.get("name", ""),
            "party": row.get("party", ""),
            "state": row.get("state", ""),
            "chamber": row.get("chamber", ""),
            "score": _int_or_zero(row.get("notability_score")),
            "headline": _str_or_default(row.get("headline"), ""),
            "stance_count": _int_or_zero(rank_by_id.loc[eid, "stance_drift_count"]) if eid in rank_by_id.index else 0,
            "topical_count": _int_or_zero(rank_by_id.loc[eid, "topical_drift_count"]) if eid in rank_by_id.index else 0,
            "filename": f"{bid}.html",
        })

    _render_index(index_rows, output_dir, total_members=len(drift_brief))
    return pd.DataFrame(out_rows)
