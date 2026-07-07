"""Spike part 2: does PyMuPDF search_for find quotes that span the PDF's shattered
table text? Try candidate quote shapes a model might emit, from friendly to hostile."""
import fitz

PDF = r"C:\journalism_sprint\prototype_one_palm_wt\build\palm_tier2_cache\ca63caae629742d2.pdf"
doc = fitz.open(PDF)

CANDIDATES = [
    # (label, quote, page hint 1-indexed)
    ("contiguous near-sentence (p160 remark)",
     "total CPO Production 52,228.29 MT vs total sold 43,933.88 MT", 160),
    ("header phrase split by newline (p8)",
     "Actual Production for this Audit Year (MT)", 8),
    ("table cells run: three numbers on separate lines (p8)",
     "229,372 52,228 14,221", 8),
    ("header + cells mixed, the 'natural' model quote (p8)",
     "Actual Production for this Audit Year (MT) Aug’22 –Jul’23 FFB CPO PK 243,247.5 65,677 16,419 229,372 52,228 14,221", 8),
    ("p158 confirmation row",
     "BKL POM 52,228 14,221 Actual volumes between Aug’22 to Jul’23", 158),
]

for label, quote, pno in CANDIDATES:
    page = doc[pno - 1]
    rects = page.search_for(quote)
    print(f"[{'HIT ' if rects else 'MISS'}] {label}")
    print(f"       quote: {quote[:80]}{'...' if len(quote) > 80 else ''}")
    if rects:
        print(f"       page {pno}: {len(rects)} rect(s), first: {rects[0]}")
    else:
        # diagnose: try whole-doc search in case the page hint is wrong
        other = [i + 1 for i, p in enumerate(doc) if p.search_for(quote)]
        print(f"       whole-doc pages with hit: {other or 'none'}")
    print()
