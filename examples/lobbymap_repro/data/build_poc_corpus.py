"""
Build the POC scoring-targets table for the lobbymap_repro DAG.

Selects SMALL cells (<=2 evidence items) from the ingested ground truth — they
span -2..+2 across 5 entities — and fetches each evidence item's ORIGINAL-
PUBLISHER source document (we skip lobbymap.org-hosted copies: robots.txt bans
automated agents on their site; the original publishers are fair game).

Output: scoring_targets.parquet — one row per (evidence source doc × cell), with
the document text inline. Rows whose document could not be fetched are written to
unfetched.jsonl instead (fail loudly, never silently).
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

HERE = Path(__file__).resolve().parent
GT = HERE.parent.parent / "lobbymap" / "eval" / "ground_truth"
MAX_EVIDENCE_PER_CELL = 2
RETRIEVED_AT = "2026-07-01T00:00:00"

QUERY_NAMES = {}  # filled from raw_cells (their own query naming, not invented)


def fetch_text(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=45, headers={"User-Agent": "Mozilla/5.0 (research; journalism)"})
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"  FETCH FAIL {url[:80]} — {type(exc).__name__}")
        return None
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(r.content))
            return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        except Exception as exc:  # noqa: BLE001
            print(f"  PDF PARSE FAIL {url[:80]} — {type(exc).__name__}")
            return None
    soup = BeautifulSoup(r.text, "lxml")
    for t in soup(["script", "style", "nav", "footer", "header"]):
        t.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))


LOCAL_DOCS = HERE / "local_docs"


def extract_local(path: Path) -> str | None:
    data = path.read_bytes()
    if data[:4] == b"%PDF":
        try:
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        except Exception:  # noqa: BLE001
            return None
    soup = BeautifulSoup(data.decode("utf-8", errors="replace"), "lxml")
    for t in soup(["script", "style"]):
        t.decompose()
    return soup.get_text("\n", strip=True)


def calpers_targets() -> list[dict]:
    """The already-built CalPERS corpus (original-publisher PDFs) as targets."""
    docs_file = GT.parent.parent / "data" / "documents.jsonl"
    if not docs_file.exists():
        return []
    out = []
    for line in docs_file.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        out.append({
            "target_id": f"C:california_public_employees_retirement_system_calpers::Q2::D2::{d['doc_id']}",
            "entity_id": "C:california_public_employees_retirement_system_calpers",
            "entity_name": "California Public Employees Retirement System (CalPERS)",
            "entity_kind": "company", "query_id": "Q2",
            "query_name": "Climate Science Stance: Does the organization support a science-based response to the climate crisis?",
            "source_id": "D2", "source_name": "Corporate Media",
            "evidence_year": d["published_date"][:4],
            "url": d["url"], "retrieved_at": d["retrieved_at"], "doc_text": d["raw_text"][:60000],
        })
    return out


def main() -> None:
    cells = [json.loads(line) for line in (GT / "raw_cells.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    small = [c for c in cells if 1 <= c["n_evidence"] <= MAX_EVIDENCE_PER_CELL]
    print(f"{len(small)} small cells selected of {len(cells)}:")
    rows, unfetched = [], []
    for c in small:
        print(f"  {c['entity_name'][:22]:22} {c['query_id']}/{c['source_id']} cell={c['cell_score']} n={c['n_evidence']}")
        for i, e in enumerate(c["evidence"]):
            publisher_links = [u for u in e["source_links"] if "lobbymap.org" not in u]
            rec_base = {
                "target_id": f"{c['entity_id']}::{c['query_id']}::{c['source_id']}::{i}",
                "entity_id": c["entity_id"], "entity_name": c["entity_name"],
                "entity_kind": c["entity_kind"],
                "query_id": c["query_id"], "query_name": c["query_name"] or e.get("query") or "",
                "source_id": c["source_id"], "source_name": c["source_name"] or e.get("source") or "",
                "evidence_year": e.get("year"),
            }
            local = LOCAL_DOCS / Path(e["source_links"][0]).name if e["source_links"] else None
            if not publisher_links:
                # lobbymap-hosted copy only. We do NOT fetch from lobbymap.org —
                # the human can click-download it; we pick it up from LOCAL_DOCS.
                if local is not None and local.exists():
                    text = extract_local(local)
                    if text and len(text) >= 200:
                        rows.append({**rec_base, "url": "manual:" + local.name,
                                     "retrieved_at": RETRIEVED_AT, "doc_text": text[:60000]})
                        continue
                unfetched.append({**rec_base,
                                  "lobbymap_hosted": "https://lobbymap.org" + e["source_links"][0].replace("//", "/", 1) if e["source_links"] else None,
                                  "reason": "lobbymap-hosted only; download manually to data/local_docs/"})
                continue
            url = publisher_links[0]
            text = fetch_text(url)
            if not text or len(text) < 200:
                unfetched.append({**rec_base, "url": url, "reason": "fetch failed or too short"})
                continue
            rows.append({**rec_base, "url": url, "retrieved_at": RETRIEVED_AT,
                         "doc_text": text[:60000]})
    for r in rows:
        r["context_kind"] = "source_document"
    # EVIDENCE-TIER targets: every gt evidence row with a usable verbatim extract.
    # Scores THEIR extract (tests the rubric, not extraction) — no fetching needed.
    n_skipped = 0
    for line in (GT / "gt_scored_evidence.jsonl").read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        ext = e.get("source_extract") or ""
        if len(ext) < 80:
            n_skipped += 1
            continue
        eid = e.get("company_id") or e.get("influencer_id")
        rows.append({
            "target_id": "EV::" + e["evidence_id"],
            "entity_id": eid, "entity_name": eid.split(":", 1)[1].replace("_", " ").title(),
            "entity_kind": "influencer" if e.get("influencer_id") else "company",
            "query_id": e["query_id"], "query_name": "",
            "source_id": e["source_id"], "source_name": "",
            "evidence_year": e.get("year"), "url": e.get("evidence_url") or "",
            "retrieved_at": RETRIEVED_AT, "doc_text": ext[:60000],
            "context_kind": "im_extract",
        })
    print(f"evidence-tier targets added (skipped {n_skipped} with no/short extract)")
    # query names: take them from raw_cells so the prompt has the real query text
    qnames = {}
    for line in (GT / "raw_cells.jsonl").read_text(encoding="utf-8").splitlines():
        c = json.loads(line)
        if c.get("query_id") and c.get("query_name"):
            qnames[c["query_id"]] = c["query_name"]
    for r in rows:
        if not r["query_name"]:
            r["query_name"] = qnames.get(r["query_id"], r["query_id"])
    df = pd.DataFrame(rows)
    df.to_parquet(HERE / "scoring_targets.parquet", index=False)
    (HERE / "unfetched.jsonl").write_text(
        "\n".join(json.dumps(u, ensure_ascii=False) for u in unfetched) + ("\n" if unfetched else ""),
        encoding="utf-8")
    print(f"\nfetched {len(df)} target docs; {len(unfetched)} unfetched (see unfetched.jsonl)")
    if len(df):
        print(df[["entity_name", "query_id", "source_id", "url"]].to_string(max_colwidth=60))


if __name__ == "__main__":
    main()
