"""
Ingest manually-saved LobbyMap pages into structured ground truth.

We parse pages a human saved from lobbymap.org — we do NOT crawl the site
(robots.txt disallows /evidence/ + /score/ and bans automated agents).

Three page types, auto-detected by content:
  CELL page       — one (entity, query, source) cell with scored evidence items.
                    Titles like "LobbyMap Exxon Mobil - Q1 : D5".
  SCORECARD page  — an entity's full profile: the matrix table (every cell's
                    score/NS/NA) + header scores. Titles like "LobbyMap Clean
                    Energy Council".
  TECHNOLOGY page — a Science-Based Benchmark page ("LobbyMap Oil").

Duplicates (" (1)" copies etc.) are deduped by content hash.

OUTPUT (eval/ground_truth/):
  raw_cells.jsonl          — full-fidelity cell records (provenance)
  gt_scored_evidence.jsonl — their per-evidence rows
  gt_cell_score.jsonl      — their per-cell rows (cell pages + matrix tables)
  gt_entity_scores.jsonl   — entity-level header scores (org score, band, ...)
  benchmarks.jsonl         — Science-Based Benchmark text per technology

Nothing fabricated; missing fields are null.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
OUT = HERE / "ground_truth"

TITLE_CELL_RE = re.compile(r"^LobbyMap (?P<entity>.+?) - Q(?P<q>\d+) : D(?P<d>\d+)$")

# Matrix column order observed on scorecard pages (D1..D7).
SOURCE_ORDER = ["Main Web Site", "Corporate Media", "CDP Responses",
                "Direct Consultation", "Media Reports", "CEO Messaging",
                "Financial Disclosures"]


def _txt(el: Any) -> Optional[str]:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else None


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def _soup(path: Path) -> BeautifulSoup:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "lxml")
    for t in soup(["script", "style", "svg"]):
        t.decompose()
    return soup


def _entity_kind(soup: BeautifulSoup, entity_name: str) -> str:
    """company vs influencer, from the page's own links to its profile."""
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        if "/influencer/" in href and _slug(entity_name)[:12] in _slug(href):
            return "influencer"
    return "company"


def classify(soup: BeautifulSoup) -> str:
    title = _txt(soup.title) or ""
    if TITLE_CELL_RE.match(title):
        return "cell"
    if soup.select_one("td.score-cell"):
        return "scorecard"
    if "Science-Based Policy Benchmark" in soup.get_text(" ", strip=True):
        return "technology"
    return "unknown"


# ── CELL pages ────────────────────────────────────────────────────────────────

def parse_cell(path: Path, soup: BeautifulSoup) -> dict[str, Any]:
    m = TITLE_CELL_RE.match(_txt(soup.title) or "")
    assert m, f"not a cell page: {path.name}"
    entity_name = m.group("entity")
    query_id, source_id = f"Q{m.group('q')}", f"D{m.group('d')}"
    kind = _entity_kind(soup, entity_name)

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
        comment = it.select_one(".card-evidence-item-comment")
        extract = it.select_one(".card-evidence-item-extract")
        links = sorted({a["href"] for a in it.select("a[href]")
                        if "card-evidence-item-title-link" not in (a.get("class") or [])})
        items.append({
            "score": it.get("data-score"),
            "year": _txt(it.select_one(".card-evidence-item-title-score")),
            "region": info.get("region"),
            "source": info.get("source"),
            "query": info.get("query"),
            "im_comment": " ".join(p.get_text(" ", strip=True) for p in comment.select("p")).strip() if comment else None,
            "source_extract": _txt(extract.select_one("p")) if extract else None,
            "source_links": links,
        })
    return {
        "source_file": path.name,
        "entity_id": ("I:" if kind == "influencer" else "C:") + _slug(entity_name),
        "entity_name": entity_name, "entity_kind": kind,
        "query_id": query_id, "query_name": items[0]["query"] if items else None,
        "source_id": source_id, "source_name": items[0]["source"] if items else None,
        "cell_score": cell_score, "n_evidence": len(items), "evidence": items,
    }


# ── SCORECARD pages (full matrix + header scores) ─────────────────────────────

def parse_scorecard(path: Path, soup: BeautifulSoup) -> dict[str, Any]:
    entity_name = _txt(soup.select_one("h1")) or path.stem
    kind = _entity_kind(soup, entity_name)
    eid = ("I:" if kind == "influencer" else "C:") + _slug(entity_name)

    # Header scores: label/value pairs appear as text runs like
    # "InfluenceMap Score ... B+ Performance Band 83% Organization Score ..."
    header_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:4000]
    scores: dict[str, Any] = {}
    for label, key in [("Performance Band", "performance_band"),
                       ("Organization Score", "organization_score"),
                       ("Relationship Score", "relationship_score"),
                       ("Engagement Intensity", "engagement_intensity")]:
        mm = re.search(r"([\w+\-.]+%?)\s*" + re.escape(label), header_text)
        scores[key] = mm.group(1) if mm else None

    # The matrix: row 2 holds source-column headers; data rows = query name + 7 cells.
    cells = []
    table = soup.select_one("table")
    if table is not None:
        rows = table.select("tr")
        for tr in rows:
            tds = tr.select("td")
            th = _txt(tr.select_one("th")) or (_txt(tds[0]) if tds else None)
            score_cells = tr.select("td.score-cell")
            if not score_cells or not th or th.upper() == "QUERIES":
                continue
            for j, td in enumerate(score_cells):
                raw = _txt(td)
                link = td.select_one("a.score-link")
                href = link.get("href") if link else None
                qd = re.search(r"-Q(\d+)-D(\d+)", href or "")
                cells.append({
                    "query_name": th,
                    "query_id": f"Q{qd.group(1)}" if qd else None,
                    "source_id": f"D{qd.group(2)}" if qd else (f"D{j+1}" if j < 7 else None),
                    "source_name": td.get("data-title") or (SOURCE_ORDER[j] if j < len(SOURCE_ORDER) else None),
                    "raw": raw,
                })
    return {"source_file": path.name, "entity_id": eid, "entity_name": entity_name,
            "entity_kind": kind, "scores": scores, "matrix_cells": cells}


# ── TECHNOLOGY (benchmark) pages ─────────────────────────────────────────────

def parse_technology(path: Path, soup: BeautifulSoup) -> dict[str, Any]:
    tech = _txt(soup.select_one("h1")) or path.stem
    # Benchmark prose: paragraphs between the h1 block and the global footer nav.
    h1 = soup.select_one("h1")
    paras: list[str] = []
    node = h1.find_parent() if h1 else None
    seen_footer = False
    if node is not None:
        for p in node.find_all_next("p"):
            t = _txt(p) or ""
            if not t:
                continue
            if t.startswith(("LobbyMap maintains", "InfluenceMap's programs")):
                seen_footer = True
                break
            paras.append(t)
            if len(paras) >= 12:
                break
    return {"source_file": path.name, "technology": tech,
            "benchmark_id": "B_" + _slug(tech),
            "benchmark_text": "\n\n".join(paras) if paras else None,
            "truncated": not seen_footer}


# ── Driver ───────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv] or [Path.home() / "Downloads"]
    candidates: list[Path] = []
    for r in roots:
        candidates += sorted(r.glob("*.htm")) + sorted(r.glob("*.html")) if r.is_dir() else [r]

    # Dedupe by content hash; prefer the shortest filename (the non-"(1)" copy).
    by_hash: dict[str, Path] = {}
    for p in sorted(candidates, key=lambda p: len(p.name)):
        try:
            h = hashlib.md5(p.read_bytes()).hexdigest()
        except OSError:
            continue
        by_hash.setdefault(h, p)

    cells, scorecards, benchmarks, skipped = [], [], [], []
    seen_titles: set[str] = set()
    for p in by_hash.values():
        soup = _soup(p)
        title = _txt(soup.title) or p.name
        kind = classify(soup)
        if kind == "unknown" or ("LobbyMap" not in title and kind != "cell"):
            skipped.append(p.name)
            continue
        if title in seen_titles:   # same page saved under two names, bytes differ
            continue
        seen_titles.add(title)
        if kind == "cell":
            cells.append(parse_cell(p, soup))
        elif kind == "scorecard":
            scorecards.append(parse_scorecard(p, soup))
        elif kind == "technology":
            benchmarks.append(parse_technology(p, soup))

    OUT.mkdir(parents=True, exist_ok=True)

    def dump(name: str, rows: list[dict[str, Any]]) -> None:
        (OUT / name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
            encoding="utf-8")

    dump("raw_cells.jsonl", cells)

    gt_ev, gt_cell = [], []
    for c in cells:
        is_inf = c["entity_kind"] == "influencer"
        ids = {"company_id": None if is_inf else c["entity_id"],
               "influencer_id": c["entity_id"] if is_inf else None}
        score_f = None
        try:
            score_f = float(c["cell_score"]) if c["cell_score"] not in (None, "") else None
        except ValueError:
            pass
        gt_cell.append({**ids, "query_id": c["query_id"], "source_id": c["source_id"],
                        "status": "scored" if score_f is not None else "NS",
                        "score": round(score_f) if score_f is not None else None,
                        "cell_score_raw": c["cell_score"], "provenance": c["source_file"]})
        for i, e in enumerate(c["evidence"]):
            gt_ev.append({
                "evidence_id": f"{c['entity_id']}::{c['query_id']}::{c['source_id']}::{i}",
                **ids, "query_id": c["query_id"], "source_id": c["source_id"],
                "score": int(e["score"]) if e["score"] not in (None, "") else None,
                "year": e["year"], "im_comment": e["im_comment"],
                "source_extract": e["source_extract"],
                "evidence_url": e["source_links"][0] if e["source_links"] else None,
                "source_links": e["source_links"], "provenance": c["source_file"]})

    # Matrix cells from scorecards → gt_cell_score too (status from NS/NA/number).
    for sc in scorecards:
        is_inf = sc["entity_kind"] == "influencer"
        ids = {"company_id": None if is_inf else sc["entity_id"],
               "influencer_id": sc["entity_id"] if is_inf else None}
        for cell in sc["matrix_cells"]:
            raw = (cell["raw"] or "").strip()
            status = raw if raw in ("NS", "NA") else ("scored" if raw else "NS")
            score = None
            if status == "scored":
                try:
                    score = round(float(raw))
                except ValueError:
                    status, score = "NS", None
            gt_cell.append({**ids, "query_id": cell["query_id"], "source_id": cell["source_id"],
                            "status": status, "score": score, "cell_score_raw": raw,
                            "query_name": cell["query_name"], "provenance": sc["source_file"]})

    dump("gt_scored_evidence.jsonl", gt_ev)
    dump("gt_cell_score.jsonl", gt_cell)
    dump("gt_entity_scores.jsonl",
         [{"entity_id": s["entity_id"], "entity_name": s["entity_name"],
           "entity_kind": s["entity_kind"], **s["scores"], "provenance": s["source_file"]}
          for s in scorecards])
    dump("benchmarks.jsonl", benchmarks)

    print(f"deduped {len(candidates)} files -> {len(by_hash)} unique")
    print(f"cell pages: {len(cells)}  (evidence rows: {len(gt_ev)})")
    print(f"scorecards: {len(scorecards)}  (matrix cells: {sum(len(s['matrix_cells']) for s in scorecards)})")
    print(f"benchmarks: {len(benchmarks)}")
    if skipped:
        print(f"skipped (not LobbyMap): {len(skipped)}")
    ents = sorted({c['entity_name'] for c in cells})
    print("entities (cells):", ", ".join(ents))
    for c in cells:
        print(f"  {c['entity_name'][:24]:24} {c['query_id']}/{c['source_id']}"
              f"  cell={c['cell_score']}  n={c['n_evidence']}  [{c['entity_kind']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
