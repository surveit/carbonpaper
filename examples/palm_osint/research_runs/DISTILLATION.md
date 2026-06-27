# Distilling 3 unstructured Palm research runs → a structured DAG

Raw material: 3 open-ended Claude Code research runs (transcripts in this folder),
one each for SUNGAI LILIN (Cargill/Hindoli, press-rich), BUKIT MARADJA (SIPEF,
official-report-rich), AGROWIRATAMA (Musim Mas, sparse). ~40 tool calls each
(WebSearch + WebFetch + local PDF parse). They were given NO output schema and
told to narrate process. They **converged independently** — which is the evidence
that this class of task is distillable into a stable pipeline.

## What all 3 runs agreed on (the convergent pattern)

### Source hierarchy (in order of authority)
1. **RSPO documents hosted on `rspo.org`** — the backbone. Two sub-types:
   - *Public announcement / recert notice* → current capacity, GPS, CPO/PK output,
     supply base, membership no., **PalmTrace/UML ID**, certificate currency.
   - *Full P&C / ASA public summary report* → the **PalmGHG appendix**, POME
     treatment table, OER/KER. The only place with mill-level emissions.
   - NB: fetch the **rspo.org-hosted copy**, NOT the certification body's own site
     (BSI / Control Union portals are behind auth walls / returned 403 in all 3 runs).
2. **CDM/UNFCCC registry** (only if the mill has a registered methane project) —
   the single best primary source for POME/biogas/GHG *engineering* (the PDD gives
   baseline treatment, capture tech, MW, tCO2e schedule). Hit on 2 of 3 mills.
3. **Parent annual / sustainability report** — uniquely rich *only if the parent is
   listed* (SIPEF gave per-asset FY2024 throughput); does not generalize.
4. **Press** (infosawit, sawitindonesia, biofuels-news) — used *only* for the PROPER
   rating and to disambiguate biomass-vs-POME-methane. Always graded weaker.

### Universal techniques (every run rediscovered these)
- **Key on the UML / PalmTrace ID, never the mill name.** Names collide; one PT
  bundles several mills.
- **Download-then-extract-locally.** WebFetch fails on RSPO/CDM image PDFs but saves
  the binary to disk; run `pypdf`/`pdftotext` locally and grep. This was the unlock
  in all 3 runs — a fetch tool's inline text extraction is NOT reliable here.
- **Recency ranking:** pick the newest doc by audit type + in-body date
  (RC > ASA-n.k); carry older docs forward only for fields the newest omits, labeled
  by year.
- **Fixed grep keys:** `capacity … MT/hr`, the `Summary of Net GHG`/`PalmGHG`
  appendix, `POME`/`effluent`/`anaerobic`/`methane`, `OER`/`KER`,
  `Certificate Number`, GPS, `CPO`/`Palm Kernel`. The PalmGHG appendix layout is
  standardized across RSPO reports → parse positionally.

### Consistent dead-ends (environment-bound, worth caching/routing around)
- `proper.menlhk.go.id` refused connections in all 3 → PROPER falls back to ≥2 press
  sources, flagged non-primary.
- Certification-body portals (BSI, Control Union) auth-wall / 403 → always prefer the
  rspo.org-hosted mirror.

### Irreducible judgment points (where an LLM or human MUST stay)
- **Entity disambiguation** — which mill of several under a PT; estate vs mill;
  owned-vs-supplier; certificate *scope* vs *supply base*.
- **Conflicting figures across years** (100 vs 120 t/h; 60 vs 45 t/h) — recency/basis
  judgment; a silent `max()`/`latest()` would violate provenance.
- **Designed vs operating** — a CDM-registered methane plant ≠ it ran ≠ CERs issued.
- **Biomass vs POME-methane** — press routinely conflates them.
- **Rejecting unsourced search-engine paraphrases** — the Bukit Maradja run caught a
  fabricated "valid to 2030 / CPO 29,514 t" by verifying against the actual PDF.
- **Group-level vs asset-level numbers** (the SIPEF 3.24 tCO2e/t trap).

## The distilled DAG (few LLM stages)

The ~40-tool-call exploration collapses to deterministic plumbing + 3 LLM stages:

```
 input_data      facilities (UML id, name, PT, group, coords)          [have it]
 python_transform build_queries   identity → rspo.org / cdm / proper / press queries
 ── locate ──────────────────────────────────────────────────────────────────────
 llm_transform   LOCATE           identity + search hits + prior_finds
                                   → authoritative MOST-RECENT doc url(s) + doc_type + is_new
 python_transform fetch_to_disk    download url(s)  (deterministic)
 python_transform pdf_to_text      local pypdf/pdftotext  (deterministic)
 python_transform grep_fields      fixed-key + positional PalmGHG appendix parse
 ── extract ─────────────────────────────────────────────────────────────────────
 llm_transform   EXTRACT          doc text → field set + source span + confidence + primary/press
 ── adjudicate ──────────────────────────────────────────────────────────────────
 llm_transform   ADJUDICATE       resolve identity + conflicting figures   (or human_review_queue)
 join            prior_finds      provenance memory → is_new / changed (snapshot/diff discipline)
 human_review_queue  confirm      before publish
 publish         dossiers
```

So **LLM sits at exactly 3 judgment points** (locate, extract, adjudicate); query
construction, fetching, PDF→text, grepping, and merging are deterministic. This is
the empirical answer to "how few LLM stages?" — three, and *which* three.

This also validates two design calls:
- **The connector needs an LLM** = the LOCATE stage. RSPO/CDM docs aren't at a fixed
  URL and there are several over time; finding the authoritative+recent one is
  judgment. Confirmed — all 3 runs spent real effort here.
- **"Know about past finds"** = a `prior_finds` provenance input feeding LOCATE
  (is this doc new? is it newer than what we had?) — the same snapshot/diff
  discipline the OSINT pipeline already has (`versions/` + `changelog/`).

## Transcript = spec AND eval set
Each transcript contains `(search → results)`, `(fetch → doc)`, `(doc → fields)`
pairs. They are the eval fixtures, for free:
- EXTRACT eval ← `(doc text → expected fields)` the run produced.
- LOCATE eval ← `(identity + search hits → chosen authoritative doc)`.
Distill the *structure* and harvest the *eval rows* from the same jsonl.

## Open questions for the build
- ADJUDICATE: small `llm_transform` with escalation, or a `human_review_queue`? (The
  conflicting-figure + scope cases are low-volume but high-stakes → lean review.)
- `grep_fields`: how much of the PalmGHG appendix is positionally stable enough to be
  pure Python vs needs the EXTRACT LLM? (Probably: Python locates the appendix block,
  LLM reads it.)
- Connector reality: LOCATE+fetch needs real `http`/`scrape` connectors (still stubbed)
  + a PDF-cache so we don't re-download. And an egress path to `proper.menlhk.go.id`.
