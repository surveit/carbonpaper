"""
Publish a compact per-facility dossier from the ADJUDICATE output: the reconciled
fields, each with its value, confidence, source URL and primary/press grade.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

CSS = """body{font-family:-apple-system,'Segoe UI',Arial,sans-serif;max-width:900px;margin:24px auto;padding:0 24px;color:#1a1a1a}
table{width:100%;border-collapse:collapse;font-size:14px;margin:8px 0 20px}th,td{padding:7px 10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
th{background:#f0f0ed}.g-high{color:#1f5a1f;font-weight:600}.g-medium{color:#7a4a00}.g-low{color:#999}
.primary{background:#e8f8e8;border-radius:9px;padding:1px 7px;font-size:11px}.press{background:#fff4e6;border-radius:9px;padding:1px 7px;font-size:11px}
a{color:#1a3a72}code{background:#f0f0f0;padding:1px 5px;border-radius:3px}"""


def _coerce(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return []
    if hasattr(v, "tolist"):
        return list(v.tolist())
    return v or []


def _esc(s):
    return html.escape("" if s is None or (isinstance(s, float) and pd.isna(s)) else str(s))


def _rows(fields):
    out = []
    for f in fields:
        if not isinstance(f, dict):
            continue
        grade = (f.get("grade") or "").lower()
        gcls = "primary" if "prim" in grade else "press"
        url = f.get("source_url")
        src = f'<a href="{_esc(url)}">src</a>' if url else "<span class=g-low>none</span>"
        out.append(
            f"<tr><td><code>{_esc(f.get('field'))}</code></td>"
            f"<td>{_esc(f.get('value'))} {_esc(f.get('unit'))}</td>"
            f"<td class=g-{_esc(f.get('confidence'))}>{_esc(f.get('confidence'))}</td>"
            f"<td><span class={gcls}>{_esc(grade or 'n/a')}</span></td><td>{src}</td></tr>"
        )
    return "\n".join(out) or '<tr><td colspan=5 class=g-low>no fields reconciled</td></tr>'


def transform(adjudicated: pd.DataFrame, output_dir: Path | str | None = None) -> pd.DataFrame:
    output_dir = Path(output_dir or "build/palm_tier2")
    output_dir.mkdir(parents=True, exist_ok=True)
    out_rows = []
    for _, r in adjudicated.iterrows():
        fields = _coerce(r.get("reconciled_fields"))
        bid = str(r.get("facility_id")).split(":", 1)[-1]
        page = (f"<!doctype html><meta charset=utf-8><title>{_esc(r.get('name'))}</title>"
                f"<style>{CSS}</style><h1>{_esc(r.get('name'))}</h1>"
                f"<p><code>{_esc(r.get('facility_id'))}</code> · reconciled from {_esc(r.get('n_docs'))} doc(s)</p>"
                f"<table><thead><tr><th>field</th><th>value</th><th>conf</th><th>grade</th><th>source</th></tr></thead>"
                f"<tbody>{_rows(fields)}</tbody></table>"
                f"<p style='color:#888;font-size:12px'>palm_tier2 distilled DAG · {datetime.now():%Y-%m-%d %H:%M}</p>")
        path = output_dir / f"{bid}.html"
        path.write_text(page, encoding="utf-8")
        out_rows.append({"facility_id": r.get("facility_id"), "output_path": str(path),
                         "rendered_at": datetime.now().isoformat(timespec="seconds"),
                         "bytes": len(page.encode("utf-8"))})
    return pd.DataFrame(out_rows)
