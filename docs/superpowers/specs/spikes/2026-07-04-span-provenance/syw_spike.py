"""Spike: can a verbatim quote be deterministically located (page + rects) in a real
RSPO audit PDF with PyMuPDF? Also: how ambiguous is a bare value vs a quote?"""
import sys

import fitz

PDF = r"C:\journalism_sprint\prototype_one_palm_wt\build\palm_tier2_cache\ca63caae629742d2.pdf"

doc = fitz.open(PDF)
print(f"pages: {doc.page_count}")

value_pages = []
bare_60_hits = 0
for i, page in enumerate(doc):
    if page.search_for("52,228"):
        value_pages.append(i + 1)
    bare_60_hits += len(page.search_for("60"))

print(f"pages containing '52,228': {value_pages}")
print(f"occurrences of bare '60' in whole doc: {bare_60_hits}")

for pno in value_pages:
    text = doc[pno - 1].get_text()
    idx = text.find("52,228")
    print(f"--- page {pno} raw text around the figure ---")
    print(repr(text[max(0, idx - 300):idx + 150]))
