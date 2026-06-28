"""
Build the `document` corpus (Level-2 input) from downloaded source PDFs.

These are the ORIGINAL-PUBLISHER documents InfluenceMap cited as evidence for the
CalPERS Q2/D2 cell — fetched from theinvestoragenda.org (third party), not from
lobbymap.org. Output rows conform to the `document` named schema:
  doc_id, source_id, url, title, published_date, retrieved_at, raw_text
"""
from __future__ import annotations
import json
from pathlib import Path
from pypdf import PdfReader

HERE = Path(__file__).resolve().parent
PDFS = HERE / "source_pdfs"
RETRIEVED_AT = "2026-06-27T00:00:00"  # date of fetch (passed in, not invented at runtime)

# Each entry: the cited Investor Agenda statement. source_id D2 = "Corporate Media"
# (InfluenceMap's classification of this evidence's data source).
DOCS = [
    {"doc_id": "GIS_2024", "year": "2024-08",
     "title": "2024 Global Investor Statement to Governments on the Climate Crisis",
     "url": "https://theinvestoragenda.org/wp-content/uploads/2024/08/2024-Global-Investor-Statement-to-Governments-on-the-Climate-Crisis.pdf",
     "pdf": "2024_GIS.pdf"},
    {"doc_id": "GIS_2022", "year": "2022-08",
     "title": "2022 Global Investor Statement to Governments on the Climate Crisis",
     "url": "https://theinvestoragenda.org/wp-content/uploads/2022/08/2022-Global-Investor-Statement-.pdf",
     "pdf": "2022_GIS.pdf"},
    {"doc_id": "GIS_2021", "year": "2021-09",
     "title": "2021 Global Investor Statement to Governments on the Climate Crisis",
     "url": "https://theinvestoragenda.org/wp-content/uploads/2021/09/2021-Global-Investor-Statement-to-Governments-on-the-Climate-Crisis.pdf",
     "pdf": "2021_GIS.pdf"},
]


def extract_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    return "\n".join((page.extract_text() or "") for page in reader.pages).strip()


def main():
    rows = []
    for d in DOCS:
        text = extract_text(PDFS / d["pdf"])
        rows.append({
            "doc_id": d["doc_id"],
            "source_id": "D2",            # Corporate Media
            "url": d["url"],
            "title": d["title"],
            "published_date": d["year"],
            "retrieved_at": RETRIEVED_AT,
            "raw_text": text,
        })
        print(f"{d['doc_id']}: {len(text):>7,} chars  '{d['title'][:50]}'")
    out = HERE / "documents.jsonl"
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(HERE.parent.parent.parent)}")


if __name__ == "__main__":
    main()
