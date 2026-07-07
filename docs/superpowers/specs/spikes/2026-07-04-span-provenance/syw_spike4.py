"""Spike part 4: two repairs for the failed model quote.
A) Deterministic snap: given only (value, page), locate the value with PyMuPDF and
   construct the anchor from the PDF's own text — no model quote needed at all.
B) Corrective retry: tell haiku its quote wasn't verbatim, ask again, re-verify.
"""
import json
import os
import shutil
import subprocess

import fitz

PDF = r"C:\journalism_sprint\prototype_one_palm_wt\build\palm_tier2_cache\ca63caae629742d2.pdf"
OUT_PNG = r"C:\Users\shuha\AppData\Local\Temp\claude\C--journalism-sprint-prototype-one\b5ef732e-6629-4607-aa12-dc6ff38e7b05\scratchpad\syw_highlight.png"
VALUE, PAGE_HINT = "52,228", 8

doc = fitz.open(PDF)
page = doc[PAGE_HINT - 1]

print("=== A) deterministic snap from (value, page) ===")
rects = page.search_for(VALUE)
print(f"value {VALUE!r} on page {PAGE_HINT}: {len(rects)} hit(s)")
if len(rects) == 1:
    r = rects[0]
    # construct the anchor from the PDF's own words: the value's line + neighbors
    words = page.get_text("words")  # (x0,y0,x1,y1, word, block, line, word_no)
    val_word = next(w for w in words if fitz.Rect(w[:4]).intersects(r))
    block_no = val_word[5]
    block_words = [w for w in words if w[5] == block_no]
    anchor_text = " ".join(w[4] for w in block_words)
    print(f"anchor constructed from PDF text (block {block_no}):")
    print(f"  {anchor_text[:200]}")
    page.add_highlight_annot(rects)
    clip = fitz.Rect(0, max(0, r.y0 - 140), page.rect.width, min(page.rect.height, r.y1 + 140))
    pix = page.get_pixmap(dpi=150, clip=clip)
    pix.save(OUT_PNG)
    print(f"highlight rendered: {OUT_PNG}")
else:
    print("ambiguous on page -> would need disambiguation (quote or row context)")

print()
print("=== B) corrective retry with haiku ===")
page_text = doc[PAGE_HINT - 1].get_text()
prompt = f"""From the following text extracted from page {PAGE_HINT} of an RSPO audit PDF,
find the mill's ACTUAL CPO production for the audit year (not projected, not forecast).

Your previous answer gave the quote "Actual Production for this Audit Year (MT) CPO 52,228"
but that is NOT a verbatim run of the page text - you merged a header, a column label and
a cell. A verbatim quote must be an EXACT contiguous substring of the page text below
(ignoring line breaks). Copy characters exactly; do not reorder or combine distant parts.

Return ONE JSON object only (no prose, no fences):
{{"value": "<the figure>", "unit": "<unit>", "quote": "<exact contiguous run, max 15 words>"}}

PAGE TEXT:
{page_text}"""

env = {k: v for k, v in os.environ.items()
       if not (k.startswith("CLAUDE_CODE") or k == "CLAUDECODE" or k.startswith("ANTHROPIC_"))}
proc = subprocess.run([shutil.which("claude"), "-p", "--output-format", "json", "--model", "haiku"],
                      input=prompt, capture_output=True, text=True, encoding="utf-8",
                      timeout=180, env=env)
envelope = json.loads(proc.stdout)
reply = envelope["result"].strip()
if reply.startswith("```"):
    reply = reply.strip("`").lstrip("json").strip()
answer = json.loads(reply)
print("retry reply:", json.dumps(answer, indent=1))
hits = [(i + 1, doc[i].search_for(answer["quote"])) for i in range(doc.page_count)]
hits = [(p, r) for p, r in hits if r]
print(f"retry verify: {'VERIFIED on page(s) ' + str([p for p, _ in hits]) if hits else 'FAIL - still not verbatim'}")
