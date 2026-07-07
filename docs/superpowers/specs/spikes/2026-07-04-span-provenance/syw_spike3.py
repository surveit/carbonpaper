"""Spike part 3: end-to-end cheap pathway on one real claim.
haiku (via claude -p, no API key) reads one page of extracted PDF text and emits
{value, unit, quote}; PyMuPDF then verifies the quote verbatim and renders the
highlight. Fails loudly at every step."""
import json
import os
import shutil
import subprocess
import sys

import fitz

PDF = r"C:\journalism_sprint\prototype_one_palm_wt\build\palm_tier2_cache\ca63caae629742d2.pdf"
OUT_PNG = r"C:\Users\shuha\AppData\Local\Temp\claude\C--journalism-sprint-prototype-one\b5ef732e-6629-4607-aa12-dc6ff38e7b05\scratchpad\syw_highlight.png"
PAGE_HINT = 8  # 1-indexed; the audit's production table page

doc = fitz.open(PDF)
page_text = doc[PAGE_HINT - 1].get_text()

prompt = f"""From the following text extracted from page {PAGE_HINT} of an RSPO audit PDF,
find the mill's ACTUAL CPO production for the audit year (not projected, not forecast).

Return ONE JSON object only (no prose, no fences):
{{"value": "<the figure>", "unit": "<unit>",
  "quote": "<a SHORT verbatim run of text, max 15 words, copied EXACTLY from the page,
             that contains the value — do not reorder, merge or normalize table text>"}}

PAGE TEXT:
{page_text}"""

claude_bin = shutil.which("claude")
if claude_bin is None:
    sys.exit("FAIL: claude CLI not on PATH")

env = {k: v for k, v in os.environ.items()
       if not (k.startswith("CLAUDE_CODE") or k == "CLAUDECODE" or k.startswith("ANTHROPIC_"))}
proc = subprocess.run([claude_bin, "-p", "--output-format", "json", "--model", "haiku"],
                      input=prompt, capture_output=True, text=True, encoding="utf-8",
                      timeout=180, env=env)
if proc.returncode != 0:
    sys.exit(f"FAIL: claude -p exit={proc.returncode}: {(proc.stderr or '')[:500]}")

envelope = json.loads(proc.stdout)
if envelope.get("is_error"):
    sys.exit(f"FAIL: claude -p error: {envelope.get('result', '')[:300]}")
reply = envelope["result"].strip()
if reply.startswith("```"):
    reply = reply.strip("`").lstrip("json").strip()
answer = json.loads(reply)
print("model reply:", json.dumps(answer, indent=1))

quote = answer["quote"]
hits = [(i + 1, doc[i].search_for(quote)) for i in range(doc.page_count)]
hits = [(p, r) for p, r in hits if r]
if not hits:
    sys.exit(f"VERIFY FAIL: quote not found verbatim anywhere in the PDF: {quote!r}")

print(f"VERIFIED: quote found on page(s) {[p for p, _ in hits]}")
pno, rects = hits[0]
page = doc[pno - 1]
page.add_highlight_annot(rects)
clip = fitz.Rect(0, max(0, rects[0].y0 - 120), page.rect.width, min(page.rect.height, rects[-1].y1 + 120))
pix = page.get_pixmap(dpi=150, clip=clip)
pix.save(OUT_PNG)
print(f"highlight rendered: {OUT_PNG} (page {pno}, {len(rects)} rect(s))")
