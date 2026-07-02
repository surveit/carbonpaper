# Show your work: claim provenance traces

**Status:** design approved in discussion 2026-07-01; implementation not started.
**Branch:** `syw-trace` (worktree `prototype_one_syw_wt`, off `palm-on-master`).

## Problem

A published dossier (e.g. `examples/palm_tier2` → per-facility HTML) asserts claims —
`cpo_production = 52,228 MT, high confidence, primary` — with a bare source link. A reader
cannot see how that value survived the pipeline: what values competed with it, which
document each came from, where the value entered the run, and which parts of the chain
are mechanical joins versus model assertions. The run outputs already contain all of
this (every stage's output table is persisted per run); nothing renders it.

## Decisions settled in discussion

1. **Audience: both, reader-facing first.** One trace record serves two renderers.
   The author-facing view is a *display depth* over the same record, not a second data
   shape. Deferred, but two constraints are taken now so it stays additive (§ trap-door
   guards).
2. **Rows-in / rows-out, no narrative layer.** For each stage on the claim's ancestral
   path, show the stage's input rows and output rows filtered to the claim's keys,
   verbatim. The pipeline has no formal "pick a row" operation at its LLM fan-ins, so
   the renderer must not invent selection semantics ("chosen", "rejected") — an earlier
   mockup did exactly that and was discarded for it. The transformation speaks for
   itself: three candidate rows in, one reconciled row out.
3. **Everything beyond raw rows must be mechanically derived** from stage declarations.
   The exhaustive list of derived annotations is in § Derived annotations. Anything not
   on that list does not appear in a trace.

## Core concepts

### Trace declaration (per stage)

What the tracer needs to know about a stage, and where it comes from:

| Piece | Source today |
|---|---|
| Stage type (`input_data`, `python_transform`, `llm_transform`, `join`, `aggregate`, `publish`) | compiled YAML `type:` |
| DAG parents | compiled YAML `inputs[].id` |
| Join keys per input/output | compiled YAML `schema.primary_key` — already declared on every palm_tier2 stage |
| **Payload path** — where sub-row data lives inside a JSON column: (column, inner key) | **new declaration.** e.g. extract: rows live in column `fields`, inner key `field`. The eval block on `13_extract.yaml` already joins on `[facility_id, field]`, implying this path without stating it. Same shape as `scorable_path` on the eval branch (PR #18). |
| Model-emitted reference columns — output columns that name an input row without being a join key (adjudicate's `source_url`) | **new declaration**, used only for display-level match annotation, never for filtering |

**v1 packaging:** a `TRACE_DECLS` map in the tracer module, example-local to palm_tier2,
holding only the two new pieces (payload paths, reference columns) plus anything the
compiled YAML lacks. Structured so each entry reads like a future `Stage` field — the
graduation into the contract is a follow-up to PR #18, not part of this work.

### The walk

Input: a claim = (run dir, stage id, key values), e.g.
(`runs/20260629T160736`, `adjudicate`, `{facility_id: palm:PO1000000054, field: cpo_production}`).

For the claim stage and then each ancestor along DAG edges:

1. **rows-out** = the stage's persisted output table, filtered to the current key
   values — via the payload path when a key lives inside a JSON column (exploding
   `reconciled_fields` / `fields` / `docs_json` to reach `field` grain).
2. **rows-in** = for each DAG parent, that parent's output table filtered by the keys
   both grains share (declared pks). Where the current keys don't exist at the parent's
   grain (walking back past a fan-in), the key *values* for the parent filter are taken
   from the key columns of the current rows-in set — e.g. the `url`s to follow back
   through `fetch_docs` are the `url`s of the extract rows that matched the claim.
3. Recurse until `input_data` stages (or a documented break — see corner cases).

The claim path in palm_tier2 (linear except one join):

```
publish ← adjudicate ← collate ← extract ← grep_fields ← pdf_to_text
        ← fetch_docs ← locate ← build_queries ← select_for_enrichment
        ← capacity_crosscheck ← {flatten_facilities ← facilities_snapshot,
                                 trase_unique ← trase_idn_mills}
```

(`coverage` is not an ancestor of `publish`; it never appears in a trace.)

Key-set evolution along that path (backward):
`(facility_id, field)` → extract explodes to `(facility_id, url, field)` →
`field` drops before extract (doc grain: `(facility_id, url)`) →
`url` originates at locate (facility grain: `(facility_id)`) →
`facility_id` originates at flatten_facilities / crosscheck (trase side keys by `uml_id`).

### Derived annotations (exhaustive)

1. **Column origin badge.** The stage where a column first materializes determines its
   badge: first materialized by an `llm_transform` → `model`; a declared join key →
   `key`; otherwise `computed`. Note this badges *keys* too: `url` is a key whose
   values were model-emitted at locate (see corner case C5).
2. **Reference matches.** For declared model-emitted reference columns only: exact
   string equality against input-row key columns. Rendered as "= row N" on match, and
   as an explicit warning ("matches no input row") on no match. Never fuzzy, never
   normalized, never used to filter.
3. **Counts.** N rows in → M rows out per hop; "showing K of N" when display is capped.

### Trace record

Raw JSON emitted next to the cooked HTML (one file per facility,
`artifacts/palm_tier2/traces/<facility_id>.json`), containing every trace for that
facility's claims. Sketch:

```json
{
  "run_id": "20260629T160736",
  "facility_id": "palm:PO1000000054",
  "claims": [
    {
      "claim_keys": {"field": "cpo_production"},
      "hops": [
        {
          "stage": "adjudicate",
          "stage_type": "llm_transform",
          "rows_out": [ {"...": "verbatim row, payload exploded"} ],
          "inputs": [
            {
              "stage": "collate",
              "filter_keys": {"facility_id": "palm:PO1000000054", "field": "cpo_production"},
              "rows_in": [ "..." ],
              "elided_cells": []
            }
          ],
          "matches": [ {"out_row": 0, "out_col": "source_url",
                        "in_row": 0, "in_col": "url", "kind": "exact_string"} ],
          "column_origins": {"value": "model", "url": "key", "note": "model"},
          "flags": []
        }
      ]
    }
  ]
}
```

`elided_cells` marks any cell withheld for size (column, char count, pointer to the
source parquet — the shape defined in C11); on the palm path this hits `doc_text` at
the pdf_to_text hop. `flags` carries the corner-case markers defined below (`chain_broken`,
`coarse_hop`, `unparseable_payload`, `pk_violation`, `no_matching_input`,
`missing_output_file`). A hop with a non-empty `flags` list renders with a visible
warning in both views.

### Renderers

**Reader (build now).** Each claim row in the dossier HTML expands to the hop cards,
newest hop first (adjudicate at top, seeds at bottom). Display-depth choices, all
lossless against the JSON: deterministic 1:1 pass-through hops (fetch_docs,
pdf_to_text) collapsed by default; URLs truncated to domain + tail; huge cells elided
per the record's elision markers. The HTML stays self-contained: the rows it renders
are inlined at publish time (no fetch, no server); the JSON file alongside is the
complete record when the inlined view has capped or collapsed something.

**Author (deferred).** Same record, full depth: every hop expanded, elided cells
resolvable against the run dir, per-stage row counts. Not built now.

**Trap-door guards (taken now):** (a) the trace record keeps stage names + filter keys
per hop so any hop can be re-joined to run outputs later; (b) the tracer is a
standalone module, `examples/palm_tier2/code/trace.py` (example-local in v1, matching
the `TRACE_DECLS` packaging decision), taking the declarations as data, with
`publish_tier2.py` as one caller.

## Corner cases and decided behavior

- **C1 — fan-in output that references no input row** (adjudicator synthesizes a value
  present in no candidate, or emits a URL matching nothing): rows-in/rows-out renders
  it honestly by construction; the reference-match annotation shows the explicit
  warning `no_matching_input`. This is a feature, not an error — it is the exact signal
  a reader needs.
- **C2 — empty rows-in at a hop** (0 input rows match the filter keys — e.g. a
  reconciled field no extract row supports): render "0 rows in → 1 row out" loudly.
  The walk cannot derive key values for earlier hops from an empty set, so the chain
  ends there with flag `chain_broken`. Never silently continue with unfiltered tables.
- **C3 — keys that don't exist at earlier grains** (`field` before extract, `url`
  before locate): not an error — the declared key sets shrink, and filter *values* for
  the parent come from the current rows-in (the recursive step of the walk). The trace
  visibly widens: the extract hop shows 3 rows (this field), the fetch_docs hop shows
  the 3 docs those rows came from, the locate hop shows all of the facility's docs.
- **C4 — link key not materialized across a fan-out** (locate output does not record
  which of `queries_json`'s queries produced each doc): the hop is traced at the
  coarsest shared key (`facility_id`) and flagged `coarse_hop`. The renderer says so
  ("query→doc linkage not recorded by the pipeline") rather than implying precision
  that doesn't exist. Fixing it is a pipeline change, out of scope.
- **C5 — model-emitted keys** (locate invents `url`, which every downstream stage
  treats as a key): the column-origin badge marks `url` as model-originated at the
  locate hop. Downstream of locate, joining on it is sound (deterministic stages carry
  it verbatim); *at* locate it has no ancestry, and the trace shows it originating
  there. WebSearch results behind it are not captured (v1 non-goal).
- **C6 — malformed / null JSON payloads**: fail loudly per hop — flag
  `unparseable_payload` with the raw cell preserved in the record; never coerce to `[]`
  and render "no rows". (`publish_tier2.py::_coerce` today silently returns `[]` on
  `JSONDecodeError`; the tracer must not reuse it, and that smell gets fixed when
  publish is touched.)
- **C7 — declared primary-key uniqueness violated** (two extract entries for the same
  `(facility_id, url, field)`): show all rows, flag `pk_violation`. The trace is a
  witness, not an enforcer.
- **C8 — multi-parent hops** (capacity_crosscheck joins flatten_facilities +
  trase_unique): `inputs` is a list; one rows-in table per parent, each with its own
  filter keys (`facility_id` on one side, `uml_id` on the other, per declared pks).
- **C9 — frame-grain python transforms on the path** (trase_unique dedups at frame
  grain): traced by declared keys like any other hop — frame grain means row *identity*
  is lost, not key traceability. Stages genuinely off the ancestral path (coverage)
  never enter the walk.
- **C10 — large row sets at a hop**: the JSON record holds all rows (bounded by the
  run itself); the reader renderer caps at K rows with an explicit "showing K of N —
  full set in trace JSON". No silent truncation anywhere.
- **C11 — huge cells** (`doc_text` runs to hundreds of KB): elided in the record with
  an explicit marker (column, char count, pointer to the parquet) — never dropped
  without a marker. The reader renderer shows the marker; the future author view
  resolves it.
- **C12 — partial runs / missing stage output files**: hop flagged
  `missing_output_file`, chain ends there explicitly.
- **C13 — value-format variance across sources** (`52,228` vs `678475.00`): rendered
  verbatim. No normalization for display, no fuzzy matching for the match annotation.
  If normalization is ever wanted it is a pipeline stage, visible as its own hop.
- **C14 — empty upstream payloads that are legitimate** (`field_snippets_json = {}` on
  most grep_fields rows in run 20260629T160736): shown as-is; an empty payload is data,
  not an error. The reader view may collapse such hops by default (display depth only).
- **C15 — facility-selection provenance** ("why is this facility in the report at
  all"): a different claim type whose entry point is stages 01–07 rather than a
  reconciled field. The same tracer handles it (the declarations cover those stages),
  but no renderer entry point is built for it in v1.

## Relationship to PR #18 (eval data model)

- PR #18's `is_grain_preserving` is a binary veto ("declarative evals only tap nodes
  reached through grain-preserving stages"). This design needs the *relationship*, not
  the veto: per-stage key sets that extend/project across grain changes. The v1
  `TRACE_DECLS` map is deliberately shaped as that generalization, as review input for
  the contract; nothing here blocks on PR #18 landing.
- The payload path is the same concept as PR #18's `scorable_path` (a path into nested
  payloads where the meaningful rows live), and extract's existing eval block
  (`join_on: [facility_id, field]`) is the third consumer of the idea. One declaration
  should eventually serve all three (eval tap, eval join, trace).

## Testing

- Unit tests for the tracer against a committed fixture run (small synthetic outputs
  dir; offline, no LLM — matches the existing force-mock test convention). One test per
  corner case above that is constructible offline: C1, C2, C3, C6, C7, C8, C10, C11,
  C12, C13, C14.
- A smoke test that renders SYW HTML for the fixture and asserts the derived
  annotations (badges, matches, counts) against hand-computed values.
- The real run dir (`prototype_one_palm_wt/examples/palm_tier2/runs/…`) is gitignored
  and machine-local; it is a manual acceptance check, not a test dependency.

## Non-goals (v1)

- Capturing LLM prompts/responses per row (runtime change; the author view wants it
  later).
- Span-level evidence (page/offset highlights inside PDFs) — an extract-stage
  improvement.
- Fuzzy or semantic matching of any kind in the renderer.
- The author-facing viewer UI.
- Fixing the locate query→doc linkage (C4) or the loose doc scoping found during
  design (Pasir Panjang audit in Batang Kulim's doc set) — separate leads.
