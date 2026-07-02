"""
Ingest manually-saved LobbyMap cell pages into structured ground truth.

INPUT: one or more *.html files saved by a human from lobbymap.org (a single
(entity, query, data_source) "cell" page, e.g. ".../CalPERS - Q2 _ D2.html").
We parse the saved HTML — we do NOT crawl lobbymap.org (robots.txt disallows
/evidence/ and /score/, and bans automated agents). Manual save + local parse.

OUTPUT (written next to this script, under eval/ground_truth/):
  raw_cells.jsonl        — one record per saved cell, full fidelity (provenance)
  gt_scored_evidence.jsonl — their per-evidence rows (mirror of scored_evidence)
  gt_cell_score.jsonl    — their per-cell rows (mirror of cell_score)

Nothing is fabricated: every field comes from the saved page or its filename.
Missing fields are emitted as null.
"""
from __future__ import annotations
import json, re, sys, hashlib
from pathlib import Path
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
OUT = HERE / "ground_truth"

# "LobbyMap <Entity Name> - Q<n> _ D<n>.html"  (the " _ " was " : " in the title)
FNAME_RE = re.compile(r"^LobbyMap (?P<entity>.+?) - Q(?P<q>\d+) _ D(?P<d>\d+)\.html$")


def _txt(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else None


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def parse_cell(path: Path) -> dict:
    m = FNAME_RE.match(path.name)
    query_id = f"Q{m.group('q')}" if m else None
    source_id = f"D{m.group('d')}" if m else None
    entity_name_fn = m.group("entity") if m else None

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "lxml")
    for t in soup(["script", "style", "svg"]):
        t.decompose()

    cell_score = _txt(soup.select_one(".entity-profile-header-score-value-number")) \
        or _txt(soup.select_one(".entity-profile-header-score-value"))

    items = []
    for it in soup.select(".card-evidence-item"):
        info = {}
        for col in it.select(".card-evidence-item-info-col"):
            label = _txt(col.select_one(".card-evidence-item-info-label"))
            field = _txt(col.select_one(".card-evidence-item-info-field"))
            if label:
                info[label.lower()] = field
        title_link = it.select_one(".card-evidence-item-title-link")
        comment = it.select_one(".card-evidence-item-comment")
        extract = it.select_one(".card-evidence-item-extract")
        # source-document links (download / external), excluding the entity title link
        links = []
        for a in it.select("a[href]"):
            href = a.get("href")
            if href and "card-evidence-item-title-link" not in (a.get("class") or []):
                links.append(href)
        items.append({
            "score": it.get("data-score"),
            "year": _txt(it.select_one(".card-evidence-item-title-score")),
            "region": info.get("region"),
            "source": info.get("source"),
            "query": info.get("query"),
            "score_field": info.get("score"),
            "im_comment": _txt(comment.select_one("p")) if comment else None,
            "im_comment_full": " ".join(p.get_text(" ", strip=True) for p in comment.select("p")) if comment else None,
            "source_extract": _txt(extract.select_one("p")) if extract else None,
            "source_links": sorted(set(links)),
            "entity_url": title_link.get("href") if title_link else None,
        })

    entity_url = items[0]["entity_url"] if items else None
    entity_name = _txt(soup.select_one(".card-evidence-item-title-link")) or entity_name_fn
    entity_id = "C:" + _slug(entity_name_fn or entity_name or path.stem)
    return {
        "source_file": path.name,
        "entity_id": entity_id,
        "entity_name": entity_name,
        "entity_url": entity_url,
        "query_id": query_id,
        "query_name": items[0]["query"] if items else None,
        "source_id": source_id,
        "source_name": items[0]["source"] if items else None,
        "cell_score": cell_score,
        "n_evidence": len(items),
        "evidence": items,
    }


def main(argv):
    paths = []
    for a in argv or [str(Path.home() / "Downloads")]:
        p = Path(a)
        paths += sorted(p.glob("LobbyMap*Q*_*D*.html")) if p.is_dir() else [p]
    if not paths:
        print("no LobbyMap cell HTML files found", file=sys.stderr); return 1
    OUT.mkdir(parents=True, exist_ok=True)
    cells = [parse_cell(p) for p in paths]

    (OUT / "raw_cells.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cells) + "\n", encoding="utf-8")

    gt_ev, gt_cell = [], []
    for c in cells:
        gt_cell.append({
            "company_id": c["entity_id"], "influencer_id": None,
            "query_id": c["query_id"], "source_id": c["source_id"],
            "status": "scored" if c["cell_score"] not in (None, "") else "NS",
            "score": None if c["cell_score"] in (None, "") else round(float(c["cell_score"])),
            "cell_score_raw": c["cell_score"],
        })
        for i, e in enumerate(c["evidence"]):
            eid = f"{c['entity_id']}::{c['query_id']}::{c['source_id']}::{i}"
            gt_ev.append({
                "evidence_id": eid,
                "company_id": c["entity_id"], "influencer_id": None,
                "query_id": c["query_id"], "source_id": c["source_id"],
                "score": int(e["score"]) if e["score"] not in (None, "") else None,
                "year": e["year"], "im_comment": e["im_comment"],
                "source_extract": e["source_extract"],
                "evidence_url": e["source_links"][0] if e["source_links"] else None,
                "source_links": e["source_links"],
            })
    for name, rows in [("gt_scored_evidence", gt_ev), ("gt_cell_score", gt_cell)]:
        (OUT / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    print(f"ingested {len(cells)} cell(s): "
          f"{sum(c['n_evidence'] for c in cells)} evidence rows, {len(gt_cell)} cell rows")
    for c in cells:
        print(f"  {c['entity_name']} [{c['query_id']}/{c['source_id']}] "
              f"cell={c['cell_score']} n={c['n_evidence']}  query='{(c['query_name'] or '')[:60]}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
