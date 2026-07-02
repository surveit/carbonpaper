"""
Publish per-organisation profile pages as HTML.

Inputs (dataframes):
  org_score:              one row per entity with org_score, band, intensity.
  tracked_entities:       entity_id → name, jurisdiction, sectors.
  reviewed_evidence:      reviewed evidence excerpts (entity_id, query_id, final_score, quote).
  cell_breakdown:         per-(entity, source_class, query_id) cell scores for the breakdown chart.

Output: dataframe of {entity_id, output_path, rendered_at, bytes}, plus
HTML files written to `output_dir`.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

import pandas as pd


ORG_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{name} — LobbyMap profile</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", Arial, sans-serif; max-width: 880px; margin: 24px auto; padding: 0 24px; color: #1a1a1a; }}
header {{ border-bottom: 1px solid #ddd; padding-bottom: 12px; margin-bottom: 18px; }}
h1 {{ margin: 0 0 4px; }}
.meta {{ color: #666; font-size: 14px; }}
.score-card {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 18px; padding: 16px; background: #f7f7f4; border-radius: 6px; margin-bottom: 24px; }}
.score-card .field {{ font-size: 13px; color: #666; text-transform: uppercase; letter-spacing: .5px; }}
.score-card .val {{ font-size: 32px; font-weight: 600; }}
.band-A {{ color: #1f5a1f; }} .band-B {{ color: #4a7a1f; }} .band-C {{ color: #7a6a1f; }} .band-D {{ color: #7a4a1f; }} .band-E {{ color: #7a2a1f; }} .band-F {{ color: #802020; }}
.band-na {{ color: #888; }}
table.cells {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }}
table.cells th, table.cells td {{ padding: 6px 8px; border-bottom: 1px solid #eee; text-align: left; }}
table.cells th {{ background: #f0f0ed; }}
.evidence {{ background: white; border: 1px solid #ddd; border-left: 4px solid #888; padding: 12px 16px; border-radius: 4px; margin: 8px 0; }}
.evidence .score {{ font-weight: 600; padding: 2px 8px; border-radius: 3px; background: #f0f0ed; }}
.evidence blockquote {{ margin: 8px 0; font-style: italic; color: #444; border-left: 2px solid #ccc; padding-left: 12px; }}
.evidence .src {{ font-size: 12px; color: #888; }}
footer {{ margin-top: 32px; color: #888; font-size: 12px; border-top: 1px solid #ddd; padding-top: 12px; }}
</style>
</head>
<body>
<header>
  <h1>{name}</h1>
  <div class="meta">{kind} · {jurisdiction} · sectors: {sectors}</div>
</header>

<section class="score-card">
  <div><div class="field">Org Score</div><div class="val band-{band_class}">{org_score}</div></div>
  <div><div class="field">Band</div><div class="val band-{band_class}">{band}</div></div>
  <div><div class="field">Engagement Intensity</div><div class="val">{intensity:.1f} <span style="font-size:14px">({intensity_label})</span></div></div>
</section>

<h2>Per-query breakdown</h2>
{cells_table}

<h2>Reviewed evidence</h2>
{evidence_blocks}

<footer>
  Rendered {rendered_at} · methodology: lobbymap · run-time generated
</footer>
</body>
</html>
"""


def transform(
    org_score: pd.DataFrame,
    tracked_entities: pd.DataFrame,
    reviewed_evidence: pd.DataFrame,
    cell_breakdown: pd.DataFrame,
    output_dir: str = "build/profiles",
) -> pd.DataFrame:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Join entity metadata onto the score table
    df = org_score.merge(tracked_entities, on="entity_id", how="left")

    rows = []
    for _, r in df.iterrows():
        eid = r["entity_id"]
        cells = cell_breakdown[cell_breakdown["entity_id"] == eid]
        evidence = reviewed_evidence[reviewed_evidence["entity_id"] == eid]
        page = _render_one(r, cells, evidence)
        path = out_dir / f"{_slug(eid)}.html"
        path.write_text(page, encoding="utf-8")
        rows.append({
            "entity_id": eid,
            "output_path": str(path),
            "rendered_at": datetime.now().isoformat(timespec="seconds"),
            "bytes": len(page.encode("utf-8")),
        })

    # Index page
    _write_index(out_dir, df)

    return pd.DataFrame(rows)


def _render_one(meta: pd.Series, cells: pd.DataFrame, evidence: pd.DataFrame) -> str:
    band = meta.get("org_score_band")
    band_class = "na" if pd.isna(band) else band[0]  # first letter
    score = meta.get("org_score")
    score_str = "n/a" if pd.isna(score) else f"{score:.0f}"

    cells_html = _render_cells(cells)
    evidence_html = _render_evidence(evidence)

    sectors = meta.get("sectors") or []
    if not isinstance(sectors, list):
        sectors = [str(sectors)]

    return ORG_TEMPLATE.format(
        name=html.escape(str(meta.get("name", meta["entity_id"]))),
        kind=html.escape(str(meta.get("entity_kind", "?"))),
        jurisdiction=html.escape(str(meta.get("jurisdiction") or "—")),
        sectors=html.escape(", ".join(str(s) for s in sectors) or "—"),
        org_score=score_str,
        band=html.escape(str(band) if not pd.isna(band) else "n/a"),
        band_class=band_class,
        intensity=meta.get("engagement_intensity", 0.0),
        intensity_label=html.escape(str(meta.get("intensity_label", "—"))),
        cells_table=cells_html,
        evidence_blocks=evidence_html,
        rendered_at=datetime.now().isoformat(timespec="seconds"),
    )


def _render_cells(cells: pd.DataFrame) -> str:
    if cells.empty:
        return "<p><em>No per-cell data available.</em></p>"
    rows = []
    for _, c in cells.iterrows():
        cs = c.get("cell_score")
        cs_str = "—" if pd.isna(cs) else f"{cs:+.2f}"
        rows.append(
            f"<tr><td><code>{html.escape(c['query_id'])}</code></td>"
            f"<td>{html.escape(c['source_class'])}</td>"
            f"<td>{cs_str}</td>"
            f"<td>{c.get('evidence_count', 0)}</td></tr>"
        )
    return (
        "<table class='cells'>"
        "<thead><tr><th>policy query</th><th>source</th><th>cell score</th><th>n</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_evidence(evidence: pd.DataFrame) -> str:
    if evidence.empty:
        return "<p><em>No reviewed evidence excerpts to show.</em></p>"
    blocks = []
    for _, e in evidence.iterrows():
        score = e.get("final_score")
        # final_score is a -2..+2 integer but pandas stores it as float once the
        # column carries any NaN; `:+d` rejects floats, so coerce after the null check.
        score_str = "—" if pd.isna(score) else f"{int(round(score)):+d}"
        blocks.append(
            f"<div class='evidence'>"
            f"<div><span class='score'>{score_str}</span> · "
            f"<code>{html.escape(e.get('query_id', '?'))}</code></div>"
            f"<blockquote>{html.escape(str(e.get('quote', '')))}</blockquote>"
            f"<div class='src'>evidence_id <code>{html.escape(str(e.get('evidence_id', '?')))}</code></div>"
            f"</div>"
        )
    return "\n".join(blocks)


def _write_index(out_dir: Path, df: pd.DataFrame) -> None:
    rows = []
    for _, r in df.sort_values("org_score", ascending=False, na_position="last").iterrows():
        score = r.get("org_score")
        score_str = "n/a" if pd.isna(score) else f"{score:.0f}"
        rows.append(
            f"<tr><td><a href='{_slug(r['entity_id'])}.html'>"
            f"{html.escape(str(r.get('name', r['entity_id'])))}</a></td>"
            f"<td>{html.escape(str(r.get('entity_kind', '?')))}</td>"
            f"<td>{html.escape(str(r.get('jurisdiction') or '—'))}</td>"
            f"<td>{score_str}</td>"
            f"<td>{html.escape(str(r.get('org_score_band') or 'n/a'))}</td></tr>"
        )
    index = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>LobbyMap profiles index</title>"
        "<style>body{font-family:-apple-system,sans-serif;max-width:800px;margin:24px auto;padding:0 24px;}"
        "table{width:100%;border-collapse:collapse;}th,td{padding:8px 10px;border-bottom:1px solid #eee;text-align:left;}"
        "th{background:#f0f0ed;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:#666}</style>"
        "</head><body><h1>LobbyMap profiles</h1>"
        "<table><thead><tr><th>name</th><th>kind</th><th>jurisdiction</th><th>org score</th><th>band</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></body></html>"
    )
    (out_dir / "index.html").write_text(index, encoding="utf-8")


def _slug(s: str) -> str:
    return s.replace(":", "_").replace("/", "_").replace(" ", "_")
