"""
Render per-member HTML profile pages.

For each member we emit:
  - header (name, party, state, chamber, press release count)
  - per-query scorecard with cell_score, evidence count, top quotes
  - lobbying context: top 3 lobbying clients on each issue
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{name} ({party}-{state}) — CongressWatch profile</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", Arial, sans-serif; max-width: 920px; margin: 24px auto; padding: 0 24px; color: #1a1a1a; line-height: 1.5; }}
header {{ border-bottom: 1px solid #ddd; padding-bottom: 12px; margin-bottom: 18px; }}
h1 {{ margin: 0 0 4px; }}
.party-D {{ color: #1f4a8a; }}
.party-R {{ color: #8a1f1f; }}
.party-I {{ color: #4a4a4a; }}
.meta {{ color: #666; font-size: 14px; }}
.disclaimer {{ background: #fffbe6; border: 1px solid #d4c060; padding: 10px 14px; border-radius: 4px; margin: 14px 0; font-size: 13px; color: #5a4a00; }}
.query-card {{ background: #f7f7f4; border-radius: 6px; padding: 14px 18px; margin: 14px 0; }}
.query-card h2 {{ margin: 0 0 6px; font-size: 16px; }}
.poles {{ font-size: 12px; color: #555; margin-bottom: 8px; }}
.score-block {{ display: flex; gap: 24px; align-items: center; margin-bottom: 10px; }}
.score-block .num {{ font-size: 36px; font-weight: 600; }}
.score-pos2 {{ color: #1f5a1f; }} .score-pos1 {{ color: #4a7a1f; }}
.score-neg2 {{ color: #802020; }} .score-neg1 {{ color: #7a4a1f; }}
.score-zero {{ color: #555; }} .score-na   {{ color: #999; }}
.evidence {{ background: white; border: 1px solid #ddd; border-left: 4px solid #888; padding: 10px 14px; margin: 6px 0; font-size: 13px; }}
.evidence blockquote {{ margin: 4px 0; font-style: italic; color: #444; border-left: 2px solid #ccc; padding-left: 10px; }}
.evidence .src {{ font-size: 11px; color: #888; }}
.lobby-context {{ background: #fff; border: 1px solid #ddd; padding: 10px 14px; margin-top: 8px; border-radius: 4px; font-size: 13px; }}
.lobby-context strong {{ color: #444; }}
footer {{ margin-top: 32px; color: #888; font-size: 12px; border-top: 1px solid #ddd; padding-top: 12px; }}
</style>
</head>
<body>
<header>
  <h1 class="party-{party_class}">{name}</h1>
  <div class="meta">{party} · {state} · {chamber} · {press_count} press release{press_s} in slice window</div>
</header>

<div class="disclaimer">
  <strong>What this is:</strong> public stance scores derived from press releases,
  shown alongside lobbying activity on the same issues. The page does NOT claim
  the member is influenced by these lobbyists — it shows the rhetorical and
  lobbying landscape side-by-side so readers can form hypotheses worth
  investigating.
</div>

{query_sections}

<footer>Generated {generated_at} · CongressWatch v1 prototype</footer>
</body>
</html>
"""


def _score_class(score):
    if score is None or pd.isna(score):
        return "na"
    if score >= 1.5: return "pos2"
    if score >= 0.5: return "pos1"
    if score <= -1.5: return "neg2"
    if score <= -0.5: return "neg1"
    return "zero"


def _score_display(score) -> str:
    if score is None or pd.isna(score):
        return "—"
    return f"{score:+.1f}"


def transform(
    cell_aggregation: pd.DataFrame,
    members_universe: pd.DataFrame,
    extreme_score_review: pd.DataFrame,
    policy_queries: pd.DataFrame,
    lobbying_by_query: pd.DataFrame,
    output_dir: Path | None = None,
) -> pd.DataFrame:
    if output_dir is None:
        output_dir = Path("build/profiles")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    queries = policy_queries.set_index("query_id")
    lobby = lobbying_by_query.set_index("query_id") if not lobbying_by_query.empty else None

    out_rows = []
    index_entries = []

    for _, member in members_universe.iterrows():
        eid = member["entity_id"]
        member_cells = cell_aggregation[cell_aggregation["entity_id"] == eid]
        member_evidence = extreme_score_review[extreme_score_review["entity_id"] == eid]

        # press count from evidence (rough: distinct doc_ids implied)
        press_count = member_evidence["evidence_id"].apply(
            lambda x: x.split("::")[0] if isinstance(x, str) else ""
        ).nunique() if not member_evidence.empty else 0

        sections: list[str] = []
        for qid, q in queries.iterrows():
            cell = member_cells[member_cells["query_id"] == qid]
            score = cell["cell_score"].iloc[0] if not cell.empty else None
            evidence_count = int(cell["evidence_count"].iloc[0]) if not cell.empty else 0

            # quotes
            quote_rows = member_evidence[member_evidence["query_id"] == qid].head(3)
            quote_html = ""
            for _, qr in quote_rows.iterrows():
                quote_text = (qr.get("quote") or "")[:400]
                fs = qr.get("final_score")
                quote_html += (
                    f'<div class="evidence">'
                    f'<blockquote>{html.escape(quote_text)}</blockquote>'
                    f'<div class="src">final_score: {fs}</div>'
                    f'</div>'
                )

            # lobbying context
            lob_html = ""
            if lobby is not None and qid in lobby.index:
                row = lobby.loc[qid]
                try:
                    clients = json.loads(row["top_clients_json"])[:3]
                except Exception:
                    clients = []
                fc = int(row["filing_count"])
                spend = row["total_spend_usd"]
                if fc > 0:
                    cl_str = ", ".join(c["client"] for c in clients) if clients else "(none)"
                    spend_str = f"${spend:,.0f}" if spend else "no parsed spend"
                    lob_html = (
                        f'<div class="lobby-context"><strong>Lobbying on this issue:</strong> '
                        f'{fc} filing{"s" if fc != 1 else ""}, total parsed spend {spend_str}. '
                        f'Top clients: {cl_str}.</div>'
                    )

            sections.append(f"""
<div class="query-card">
  <h2>{html.escape(q['title'])}</h2>
  <div class="poles">+2 = {html.escape(q['left_pole'])} · −2 = {html.escape(q['right_pole'])}</div>
  <div class="score-block">
    <div class="num score-{_score_class(score)}">{_score_display(score)}</div>
    <div>{evidence_count} evidence piece{"s" if evidence_count != 1 else ""}</div>
  </div>
  {quote_html or '<p style="color:#999;font-size:13px"><em>No stance evidence in slice window.</em></p>'}
  {lob_html}
</div>
""")

        party_class = (member.get("party") or "I")[0]
        page = PAGE_TEMPLATE.format(
            name=html.escape(member.get("name", "?")),
            party=html.escape(member.get("party", "?")),
            state=html.escape(member.get("state", "?")),
            chamber=html.escape(member.get("chamber", "?")),
            party_class=party_class,
            press_count=press_count,
            press_s="" if press_count == 1 else "s",
            query_sections="\n".join(sections),
            generated_at=datetime.now().isoformat(timespec="seconds"),
        )
        bid = eid.split(":", 1)[-1]
        path = output_dir / f"{bid}.html"
        path.write_text(page, encoding="utf-8")
        out_rows.append({
            "entity_id": eid,
            "output_path": str(path),
            "rendered_at": datetime.now().isoformat(timespec="seconds"),
            "bytes": path.stat().st_size,
        })
        index_entries.append((eid, member.get("name", ""), member.get("party", ""),
                              member.get("state", ""), bid))

    # index
    index_rows = "\n".join(
        f'<tr><td><a href="{bid}.html">{html.escape(name)}</a></td>'
        f'<td>{html.escape(party)}</td><td>{html.escape(state)}</td></tr>'
        for _, name, party, state, bid in sorted(index_entries, key=lambda x: x[1])
    )
    index_html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>CongressWatch — profiles</title>
<style>body {{ font-family: -apple-system, Arial, sans-serif; max-width: 700px; margin: 24px auto; padding: 0 24px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ padding: 6px 8px; border-bottom: 1px solid #eee; text-align: left; font-size: 14px; }}
th {{ background: #f0f0ed; }}</style></head><body>
<h1>CongressWatch profiles</h1>
<p>{len(index_entries)} members. Generated {datetime.now():%Y-%m-%d}.</p>
<table><thead><tr><th>Member</th><th>Party</th><th>State</th></tr></thead><tbody>
{index_rows}
</tbody></table></body></html>"""
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")

    return pd.DataFrame(out_rows)
