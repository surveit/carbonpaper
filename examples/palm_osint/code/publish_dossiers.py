"""
Publish per-facility OSINT dossiers (HTML) + an index.

Reconstructs the human-facing record from the FLAT substrate columns (the
inverse of flatten_facilities): capacity provenance, then the Tier-2 feature
findings with their verification status. Every asserted number/feature shows
its source; unverified/unknown items are badged, never laundered into facts.

Inputs (positional, per the publish handler):
  facilities  — select_for_enrichment output (the limited set; metadata + capacity)
  enrichment  — apply_verdicts output (final, verdict-applied feature rows)
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


CSS = """
body { font-family: -apple-system, "Segoe UI", Arial, sans-serif; max-width: 960px; margin: 24px auto; padding: 0 24px; color: #1a1a1a; line-height: 1.5; }
h1 { margin-bottom: 2px; } .sub { color: #666; margin-top: 0; }
table { width: 100%; border-collapse: collapse; font-size: 14px; margin: 8px 0 20px; }
th, td { padding: 8px 10px; border-bottom: 1px solid #eee; text-align: left; vertical-align: top; }
th { background: #f0f0ed; }
.kv { background:#f7f7f4; border-left:4px solid #2a6ac8; padding:10px 16px; border-radius:4px; margin:14px 0; }
.kv div { margin: 3px 0; } .kv b { display:inline-block; min-width: 150px; color:#333; }
.badge { display:inline-block; padding:2px 9px; border-radius:11px; font-size:12px; font-weight:600; }
.b-verified { background:#e8f8e8; color:#1f5a1f; }
.b-documented_negative { background:#eef2fa; color:#1a3a72; }
.b-unknown_gap { background:#f0f0f0; color:#666; }
.disclaimer { background:#fffbe6; border:1px solid #d4c060; padding:10px 14px; border-radius:4px; margin:14px 0; font-size:13px; color:#5a4a00; }
.detail { color:#444; font-size:13px; }
a { color:#1a3a72; } code { background:#f0f0f0; padding:1px 6px; border-radius:3px; }
footer { margin-top:32px; color:#888; font-size:12px; border-top:1px solid #ddd; padding-top:12px; }
"""


def _esc(s: Any) -> str:
    return html.escape("" if s is None or (isinstance(s, float) and pd.isna(s)) else str(s))


def _coerce_list(v: Any) -> list:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return []
    if isinstance(v, list):
        return v
    if hasattr(v, "tolist"):
        return list(v.tolist())
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return [v] if v else []
    return [v]


def _cap_html(r: pd.Series) -> str:
    if pd.isna(r.get("capacity_value")):
        return '<span class="badge b-unknown_gap">no structured capacity</span> ' \
               '<span class="detail">(capacity is structured for Indonesia only, via Trase)</span>'
    url = r.get("capacity_source_url")
    src = f' — source: <a href="{_esc(url)}">{_esc(url)}</a>' if url and not pd.isna(url) else ""
    return (f'{_esc(r["capacity_value"])} <code>{_esc(r.get("capacity_unit"))}</code> '
            f'<span class="detail">(provenance={_esc(r.get("capacity_provenance"))}{src})</span>')


def _feature_rows(feats: pd.DataFrame) -> str:
    if feats.empty:
        return '<tr><td colspan="4" class="detail">No Tier-2 features survived ' \
               'verification — no on-site feature could be sourced for this mill.</td></tr>'
    out = []
    for _, f in feats.iterrows():
        status = f.get("enrichment_status", "unknown_gap")
        urls = _coerce_list(f.get("evidence_urls"))
        ev = " ".join(f'<a href="{_esc(u)}">[src]</a>' for u in urls) if urls else \
             '<span class="detail">none cited</span>'
        out.append(
            f'<tr><td><code>{_esc(f.get("feature"))}</code></td>'
            f'<td><span class="badge b-{_esc(status)}">{_esc(status)}</span> '
            f'<span class="detail">({_esc(f.get("final_confidence"))})</span></td>'
            f'<td class="detail">{_esc(f.get("detail"))}'
            f'<br><i>verdict: {_esc(f.get("verdict_reason"))}</i></td>'
            f'<td>{ev}</td></tr>'
        )
    return "\n".join(out)


def _render_facility(fac: pd.Series, feats: pd.DataFrame, output_dir: Path) -> tuple[Path, int]:
    fid = fac["facility_id"]
    bid = str(fid).split(":", 1)[-1]
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{_esc(fac['name'])} — facility dossier</title><style>{CSS}</style></head><body>
<a href="index.html">← all facilities</a>
<h1>{_esc(fac['name'])}</h1>
<p class="sub">{_esc(fac.get('owner'))} · parent: {_esc(fac.get('parent_group'))} ·
{_esc(fac.get('region'))}, {_esc(fac.get('country'))} · <code>{_esc(fid)}</code></p>

<div class="kv">
  <div><b>Nameplate capacity</b> {_cap_html(fac)}</div>
  <div><b>Location</b> {_esc(fac.get('lat'))}, {_esc(fac.get('lon'))}</div>
  <div><b>Contributing sources</b> {_esc(fac.get('n_sources'))} (multi-source: {_esc(fac.get('multi_source'))})</div>
  <div><b>Owner source</b> <a href="{_esc(fac.get('owner_source_url'))}">{_esc(fac.get('owner_source_url'))}</a></div>
</div>

<div class="disclaimer">Tier-2 on-site features below are produced by an LLM extract → adversarial
verify → human-review pipeline. <b>verified</b> = a cited source was confirmed to support the claim;
<b>documented_negative</b> = no evidence of the feature was found (absence of evidence, not proof of
absence); <b>unknown_gap</b> = not retrieved. Verify against the cited source before publication.</div>

<h2>Tier-2 on-site features</h2>
<table><thead><tr><th>Feature</th><th>Status</th><th>Detail / verdict</th><th>Evidence</th></tr></thead>
<tbody>
{_feature_rows(feats)}
</tbody></table>
<footer>Generated {datetime.now():%Y-%m-%d %H:%M} · palm_osint DAG (prototype_one)</footer>
</body></html>"""
    path = output_dir / f"{bid}.html"
    path.write_text(page, encoding="utf-8")
    return path, len(page.encode("utf-8"))


def _render_index(facilities: pd.DataFrame, enrichment: pd.DataFrame, output_dir: Path) -> None:
    counts = (enrichment.groupby("facility_id")["enrichment_status"]
              .apply(lambda s: dict(s.value_counts())) if not enrichment.empty
              else pd.Series(dtype=object))
    rows = []
    for _, fac in facilities.iterrows():
        fid = fac["facility_id"]
        bid = str(fid).split(":", 1)[-1]
        st = counts.get(fid, {}) if hasattr(counts, "get") else {}
        verified = st.get("verified", 0)
        neg = st.get("documented_negative", 0)
        gap = st.get("unknown_gap", 0)
        cap = "—" if pd.isna(fac.get("capacity_value")) else f"{fac['capacity_value']:.0f}"
        rows.append(
            f'<tr><td><a href="{_esc(bid)}.html">{_esc(fac["name"])}</a></td>'
            f'<td>{_esc(fac.get("owner"))}</td><td>{_esc(fac.get("country"))}</td>'
            f'<td>{cap}</td>'
            f'<td><span class="badge b-verified">{verified} verified</span> '
            f'<span class="badge b-documented_negative">{neg} neg</span> '
            f'<span class="badge b-unknown_gap">{gap} gap</span></td></tr>'
        )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Palm-oil facility dossiers</title><style>{CSS}</style></head><body>
<h1>Palm-oil facility dossiers</h1>
<p class="sub">{len(facilities)} facilities (Tier-2 dry run). Generated {datetime.now():%Y-%m-%d}.</p>
<table><thead><tr><th>Mill</th><th>Owner</th><th>Country</th>
<th>Capacity (t FFB/h)</th><th>Tier-2 features</th></tr></thead><tbody>
{"".join(rows)}
</tbody></table>
<footer>palm_osint DAG · prototype_one runtime</footer></body></html>"""
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def transform(facilities: pd.DataFrame, enrichment: pd.DataFrame,
              output_dir: Path | str | None = None) -> pd.DataFrame:
    output_dir = Path(output_dir or "build/palm_dossiers")
    output_dir.mkdir(parents=True, exist_ok=True)

    by_fac = dict(tuple(enrichment.groupby("facility_id"))) if not enrichment.empty else {}
    out_rows = []
    for _, fac in facilities.iterrows():
        feats = by_fac.get(fac["facility_id"], enrichment.iloc[0:0])
        path, size = _render_facility(fac, feats, output_dir)
        out_rows.append({
            "facility_id": fac["facility_id"],
            "output_path": str(path),
            "rendered_at": datetime.now().isoformat(timespec="seconds"),
            "bytes": size,
        })
    _render_index(facilities, enrichment, output_dir)
    return pd.DataFrame(out_rows)
