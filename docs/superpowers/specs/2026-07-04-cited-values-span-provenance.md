# Cited values: span-level provenance for extracted claims

**Status: exploration notes + validated spike. Nothing here is implemented.** This
document parks the "last mile" provenance idea — linking an extracted value to the
exact highlighted spot in its source document — so work can return to the structured
show-your-work design (`2026-07-01-show-your-work-design.md`, which tracks *row*
lineage across pipeline stages). The two are complementary and independent: row
lineage answers "which upstream rows produced this row"; span provenance answers
"where inside the source document did this value come from."

Terms used throughout:

- **Span**: a located region of a source document — a page number plus the
  rectangle(s) covering a run of text on that page.
- **Anchor**: the stored description of a span that lets a viewer re-find it later —
  the exact quoted text plus surrounding context and the page number (the shape of a
  W3C Web Annotation `TextQuoteSelector`; see § Landscape).
- **Extraction stage**: any LLM-driven pipeline stage that reads document text and
  emits values. E.g. in the palm_tier2 example pipeline, the `llm_transform` stage
  named `extract` reads grepped snippets of RSPO audit PDFs and emits field/value
  pairs per document.

## 1. The proposal: `CitedString` / `CitedNumber` (proposed, not designed)

A runtime-level pair of value types for columns whose values were extracted from a
source document. A `CitedString`/`CitedNumber` is the plain string or number plus a
provenance payload:

- source document URL, and the local cache path of the copy actually read
- when the copy was fetched
- the anchor (quoted text + page) and the resolved span (page + rectangles)
- verification status: whether the anchor was deterministically re-found in the
  cached copy (see § 2 — values whose anchors fail verification carry that failure
  visibly; they never silently degrade to a bare value)

In any UI table, such a value displays as the bare string/number with an affordance
to open a provenance preview: the cached document at the right page with the span
highlighted, plus the URL/fetch metadata. In stored tables it is one column value
whose payload rides alongside (representation undecided — this is the main open
design question, together with how the type interacts with schemas and with the
structured-SYW lineage record).

Why runtime-level rather than per-pipeline: every extraction stage in every
methodology has this need, and the display affordance ("click the number, see the
highlighted source") should come for free once a stage declares it emits cited
values.

## 2. The mechanism the spike validated: quote-and-verify

The provenance payload above must not be model-asserted (an LLM writing "page 36"
into a JSON field is a fabrication surface). The validated alternative:

1. The extraction LLM emits only the **value** (optionally a short verbatim quote,
   see step 3). The pipeline already knows which document — and which pages of it —
   the LLM was shown.
2. Deterministic code (PyMuPDF) searches for the value on the page(s) the LLM read.
   If the hit is unique, the anchor is **constructed from the PDF's own text** —
   quote, context, page, rectangles all come from the located hit, so the anchor is
   an observation, not a model claim.
3. If the value is ambiguous on the page, escalate: ask the model for a short
   verbatim quote containing the value and search for that. Quotes are verified by
   exact search — a quote that is not found verbatim is rejected (fail loudly,
   retry, or emit the value with a visible "no verified span" flag). Never fuzzy
   matching.
4. A claim whose value cannot be located gets no span and is visibly flagged. No
   span is ever synthesized.

This works with the runtime's existing LLM backends (`claude` CLI / Agent SDK,
subscription auth — no API key) because verification is post-hoc and local.

## 3. Spike results (2026-07-04, real data)

Target: the palm_tier2 claim `cpo_production = 52,228 MT` for facility
palm:PO1000000054 (BATANG KULIM); source = the mill's 2023 RSPO audit, a 169-page
PDF cached by the pipeline at `build/palm_tier2_cache/ca63caae629742d2.pdf`
(run 20260629T160736). Scripts preserved in `spikes/2026-07-04-span-provenance/`.

Findings:

- **Bare-value search must be page-scoped.** The string `60` occurs 403 times in
  the document; even `52,228` appears on 5 different pages. Scoped to the one page
  the model read, `52,228` was unique — the deterministic snap (step 2 above)
  located it and rendered the correct highlight (section 1.7 production table, CPO
  column) with no model quote at all.
- **PyMuPDF `search_for` survives table-shattered text.** The PDF's text layer
  stores the production table as isolated fragments (`FFB \nCPO \nPK \n243,247.5
  \n...`). Quotes spanning those line breaks still hit (4 of 5 tested quote shapes),
  returning one rectangle per line — which is the correct multi-line highlight. The
  one miss: a quote mixing the table's header phrase with cell values, which is not
  contiguous in reading order.
- **The verify step caught a real fabrication on the first try.** Haiku, asked for
  the value plus a verbatim quote, returned the correct value but a quote that
  merged header + column label + cell ("Actual Production for this Audit Year (MT)
  CPO 52,228") — text that exists nowhere in the document. Exact search rejected
  it. A corrective retry ("your quote was not verbatim; copy an exact contiguous
  run") produced a verifiable quote. This is the design's core property observed
  live: a wrong pointer cannot reach the reader, because pointers are verified
  against the source, not trusted from the model.

What the spike does **not** show: it covered one text-layer PDF and one field.
Scanned/image-only PDFs have no text layer — `search_for` will fail and an OCR
fallback (or a visible "unverifiable: no text layer" flag) is required. It also does
not close the fan-in gap from the structured-SYW design: a reconciled value's link
to its winning document remains what that design says it is; spans attach at
extraction time, per candidate value.

## 4. Prerequisites if this gets built

- **Page-aware document text.** The palm_tier2 stages `pdf_to_text`/`grep_fields`
  flatten each PDF to one text blob; the snap needs to know which page a snippet
  came from. Deterministic change, no LLM cost.
- **Fetch timestamps.** The fetch stage records the local cache path and fetch
  status per document, but not when the copy was fetched; the provenance payload
  wants that.
- **A viewer affordance.** v1 can be a link to the cached PDF with `#page=N` plus
  the quoted text shown inline; real in-browser highlighting (pdf.js) or a baked
  highlighted copy (PyMuPDF annotations, as the spike rendered) is polish on top.

## 5. Landscape (surveyed 2026-07-04 — where the wheels are)

Row-level lineage has no production-grade wheel (OpenLineage is dataset/column
level, <https://openlineage.io/docs/spec/object-model>; row lineage exists only in
research instrumentation, e.g. mlinspect,
<https://github.com/stefan-grafberger/mlinspect>; DocETL's provenance debugger is
unbuilt roadmap). Span-level citation, however, has real prior art:

- **Anthropic Citations API** — the only hosted API returning *verified* spans into
  the source document: exact `cited_text` plus char offsets (text docs) or 1-indexed
  page ranges (PDFs). <https://platform.claude.com/docs/en/docs/build-with-claude/citations>
  Constraints that shelve it here: Messages-API-only (the runtime's backends are the
  `claude` CLI / Agent SDK — using it means a new backend plus per-token billing),
  PDFs are tokenized with per-page images (expensive), and requests cap at 100 PDF
  pages — the spike's audit is 169. Its one advantage over § 2 is generation-time
  validity (no retry loop). Revisit if quote-and-verify retry rates turn out high.
- **Parser-emitted geometry** — Docling attaches `{page_no, bbox, charspan}` to
  every parsed element (<https://docling-project.github.io/docling/reference/docling_document/>);
  LlamaParse and Unstructured similar. An alternative to PyMuPDF search if the
  pipeline ever adopts a heavier parser; not needed by the spike.
- **Model-asserted citation patterns** (LangChain quote fields, LlamaIndex citation
  chunks, PaperQA2 chunk summaries) — all trust the model's pointer; rejected here
  for exactly that reason.
- **Answer-side spans** (Gemini grounding, OpenAI file_search) — locate the claim in
  the *generated answer*, not in the source; not usable for this.
- **Anchor vocabulary** — W3C Web Annotation `TextQuoteSelector` (exact + prefix +
  suffix) plus a page fragment selector is the standard shape for § 1's anchor;
  nothing to invent. <https://www.w3.org/TR/annotation-model/>
- **Highlight tooling** — PyMuPDF `search_for` → `add_highlight_annot` (validated in
  the spike); pdf.js find-controller for in-browser; RFC 8118 `#page=N` URL
  fragments for the cheap viewer.

## 6. Cost picture

The quote-and-verify pathway adds **no** LLM cost beyond what extraction already
spends (the value is being extracted anyway; verification is local string search;
occasional retries are one extra small call). The Citations API pathway would add:
API-key billing, page-image token cost per document (a ~170-page audit is on the
order of a few hundred thousand input tokens), and request-splitting for >100-page
PDFs. That asymmetry is why quote-and-verify is the default and citations are
"optional premium, maybe never."
