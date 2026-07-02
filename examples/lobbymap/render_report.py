"""Render the Level-2 eval run as a self-contained HTML report (provenance per row)."""
from __future__ import annotations
import json, html
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN = HERE / "runs" / "level2_calpers_q2_d2"
GT = HERE / "eval" / "ground_truth"


def _rows(p): return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
def esc(s): return html.escape(str(s if s is not None else ""))


def main():
    ours = _rows(RUN / "our_scored_evidence.jsonl")
    theirs = _rows(GT / "gt_scored_evidence.jsonl")
    rep = json.loads((RUN / "eval_report.json").read_text(encoding="utf-8"))
    match = rep["agreement"]["cell_score_match"]
    badge = ("#1a7f37", "MATCH") if match else ("#cf222e", "MISMATCH")

    ev_rows = ""
    for i in range(max(len(ours), len(theirs))):
        o = ours[i] if i < len(ours) else {}
        t = theirs[i] if i < len(theirs) else {}
        url = t.get("evidence_url")
        src = f'<a href="{esc(url)}" target="_blank">source ↗</a>' if url else "—"
        ev_rows += f"""<tr>
          <td>{esc(t.get('year'))}</td>
          <td class="sc">{esc(o.get('score'))}</td>
          <td class="q">{esc(o.get('quote'))[:240]}</td>
          <td class="sc">{esc(t.get('score'))}</td>
          <td class="q">{esc(t.get('source_extract'))[:240]}</td>
          <td>{src}</td></tr>"""

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>LobbyMap Level-2 eval — CalPERS Q2/D2</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#1c1c1c}}
 h1{{font-size:1.4rem;margin-bottom:.2rem}} .sub{{color:#666;margin-top:0}}
 .badge{{display:inline-block;padding:.2rem .6rem;border-radius:4px;color:#fff;font-weight:600;background:{badge[0]}}}
 table{{border-collapse:collapse;width:100%;margin:1rem 0}}
 th,td{{border:1px solid #ddd;padding:.5rem;text-align:left;vertical-align:top;font-size:13px}}
 th{{background:#f4f4f6}} .sc{{text-align:center;font-weight:700;width:3rem}} .q{{font-size:12px;color:#333}}
 .grp{{background:#eef}} .caveat{{background:#fff8e6;border:1px solid #f0d98a;border-radius:6px;padding:.8rem 1rem;margin:1rem 0}}
 .summary{{display:flex;gap:2rem;margin:1rem 0}} .summary div{{background:#f7f7f9;border-radius:6px;padding:.6rem 1rem}}
 .big{{font-size:1.6rem;font-weight:700}}
</style></head><body>
<h1>LobbyMap Level-2 eval <span class="badge">{badge[1]}</span></h1>
<p class="sub">{esc(rep['cell'])}</p>
<div class="summary">
  <div><div class="big">{esc(rep['ours']['cell_score'])}</div>our cell score</div>
  <div><div class="big">{esc(rep['ground_truth']['cell_score'])}</div>their cell score</div>
  <div><div class="big">{esc(rep['ours']['evidence_scores'])}</div>our evidence</div>
  <div><div class="big">{esc(rep['ground_truth']['evidence_scores'])}</div>their evidence</div>
</div>
<div class="caveat"><b>What this shows / doesn't.</b> Our pipeline independently scored
InfluenceMap's own cited source documents and reproduced their cell score exactly.
It <b>assumes</b> their document set (Level 2 borrows their corpus — it does not test
document discovery). It does <b>not</b> prove the methodology holds on contested cells:
this is an unambiguous cell (pro-climate investor statements both sides scored +2).
The benchmark is <b>reconstructed from the query definition, not InfluenceMap's
Science-Based Benchmarks DB</b>, so this run can't surface benchmark drift.</div>
<table>
  <tr><th>year</th><th>our score</th><th>our extracted quote</th><th>their score</th><th>their extract</th><th>src</th></tr>
  {ev_rows}
</table>
<p class="sub">Query Q2: Climate Science Stance — does the organization support a
science-based response to the climate crisis? · Source D2: Corporate Media ·
Entity: CalPERS (signatory of the cited investor statements).</p>
</body></html>"""
    out = RUN / "report.html"
    out.write_text(doc, encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    main()
