# LobbyMap Methodology — Stage Splits

This directory contains the InfluenceMap LobbyMap methodology prose split into one file per pipeline stage. Each file is a self-contained chunk for a downstream stage-compiler agent to translate into structured YAML.

**Source document:** `examples/lobbymap/methodology_raw.txt` (434 lines, scraped from <https://lobbymap.org/briefing/LobbyMap-Methodology-24422>).

## Stage index

| # | File | Stage name | Prose source line ranges | Primary methodology sections |
|---|---|---|---|---|
| 1 | `01_universe_selection.md` | Universe selection | 157, 263–279 | §1 (intro stat), §3.4 |
| 2 | `02_source_ingestion.md` | Data-source ingestion | 241–252 | §3.2, Table 3 |
| 3 | `03_evidence_extraction.md` | Evidence extraction & relevance filter | 253–262, 286–291, 428–433 | §3.3, Table 4, Appendix B |
| 4 | `04_benchmark_scoring.md` | Per-evidence benchmark scoring | 286–291, 292–330 | §4.1 intro, §4.2, §4.3, §4.4, Tables 5–7 |
| 5 | `05_importance_tagging.md` | Importance + tag assignment | 331–337 | tail of §4.4, head of §4.5 |
| 6 | `06_cell_aggregation.md` | Cell aggregation (matrix) | 338–359, 363 | §4.6, §4.7 (partial), §4.8 (partial) |
| 7 | `07_org_score_intensity.md` | Org Score + Engagement Intensity | 218, 353–365 | §2.3 (Table 2), §4.7, §4.8 |
| 8 | `08_relationship_graph.md` | Industry-association ↔ company relationship graph | 218, 366–389 | §2.3 (Relationship Score row), §4.9 |
| 9 | `09_indirect_performance_band.md` | Indirect score + Performance Band | 218, 219, 290–291 | §2.3 (Performance Band row), §4.1 final, §4.9 final |

## Notes on the methodology overall

### Stage 0 (out of scope for the pipeline build)

The methodology document devotes a substantial portion of its prose (Appendix A, lines 394–427) to **how InfluenceMap distills IPCC reports into the science-based policy benchmarks** that Stage 4 then scores against. This is a methodology-design / benchmark-authorship activity, not a per-run pipeline stage — it produces the benchmark library that the runtime pipeline consumes as a fixed input. Per the compiler-build plan, **no stage file is produced for stage 0**; the benchmark library is treated as an external resource handed to Stage 4.

Key Appendix A passages (informational only):
- Lines 394–400: rationale for using IPCC, IPCC governance, "policy-relevant but not policy-prescriptive."
- Lines 401–408: which IPCC publications are used (AR6 WG3, 1.5°C Special Report 2018, Climate Change and Land 2019).
- Lines 409–414: scenario / pathway selection (1.5°C with limited overshoot preferred).
- Lines 415–427: how findings are extracted (executive summaries, SPMs, areas of consensus across pathways).

### Cross-stage prose

A few passages describe constructs that span multiple stages and have been duplicated (with "shared with stage N" annotations) in each relevant file:

- **§2.3 Table 2 (line 218)** defines the four core metrics (Org Score, Relationship Score, Performance Band, Engagement Intensity) — appears in Stages 7, 8, 9.
- **§4.1 intro (lines 286–291)** sets up the "alignment vs intensity" framing and the direct-vs-indirect distinction — appears in Stages 3, 4, 9.
- **Line 363** (evidence intensity → cell intensity) bridges Stages 6 and 7.
- **Lines 358–359** (5-year recency cutoff) bridges Stages 5/6/7.

### Passages not assigned to any stage

The following are omitted from stage files because they are framing, marketing, or governance material with no pipeline-step content:

- Lines 1–10: page metadata / analytics scripts.
- Lines 11–144: report listings and navigation cruft from the scrape (unrelated briefings).
- Lines 145–156, 162–196: high-level mission / IPCC context / advisory group / academic citations.
- Lines 198–209 (§2.1), 210–215 (§2.2), 220–227 (§2.4 disclosure fact-checking), 228–233 (§2.5 regional tracking): describe characteristics, fact-checking workflows, and dissemination — none describe a pipeline stage. §2.4 is its own ancillary product (Disclosure Scorecards) layered on top of the LobbyMap output and not part of the 9-stage pipeline.
- Lines 234–240 (§3.1 Definition of policy engagement): definitional/conceptual.
- Lines 280–284 (§3.5 Investor expectations): stakeholder framing.
- Lines 390–393 (§4.10 / Outputs Table 8): describes output products (profiles, scorecards, regional platforms, reports), not the assessment pipeline itself.
- Line 434: organizational footer.

### Known prose ambiguities (compiler-relevant)

- **Query indexing inconsistency.** §4.7 says "policy-specific queries (Q5–Q9)"; §4.8 says "(Q6–Q11)". Both refer to the same partition (policy-level queries are higher-weighted than high-level queries) but the index ranges disagree. Likely the prose was updated when the query catalog grew; downstream compilers should treat this as a soft signal, not a hard query-ID range.
- **Query count.** User spec says "14 policy queries"; Table 4 enumerates 13 rows. Possibly the user counted with one extra disclosure split, or the original LobbyMap matrix has an off-by-one due to a sub-query. Defer to Table 4 (literal count = 13) unless evaluation data says otherwise.
- **Org Score and Performance Band aggregation formulas are underspecified.** Prose names the inputs and weights' relative ordering but does not give closed-form expressions. Stage 4–7 and Stage 9 will need either heuristic defaults or a flag-for-human-review marker.
- **D-numbering of data sources** (D1..D8) appears informally; Table 3 itself doesn't number rows. Compilers should canonicalize.
