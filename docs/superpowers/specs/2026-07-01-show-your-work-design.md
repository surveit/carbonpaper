# Show your work: claim provenance traces

**Status:** design in review; implementation not started. Revised 2026-07-02 after
discussion: lineage is now runtime-tracked at execution time, replacing an earlier
draft that reconstructed lineage from per-stage key declarations (that draft leaned on
"join keys" as if it were a pipeline-wide enforced concept; it isn't — see §3).
**Branch:** `syw-trace` (worktree `prototype_one_syw_wt`, off `palm-on-master`).

Terms used throughout, defined once here:

- **Run** — one execution of a compiled methodology DAG. The runtime persists every
  stage's output table to `runs/<run_id>/outputs/<stage_id>.parquet`.
- **Row** — a row of one of those persisted stage-output tables, identified for the
  rest of this doc as `(stage_id, row ordinal within the persisted table)`.
- **Payload** — a JSON-typed cell inside a row that itself contains a list of
  finer-grained entries. Example, from the palm_tier2 pipeline
  (`examples/palm_tier2`): the adjudicate stage's output has one row per facility,
  and that row's `reconciled_fields` cell holds a list of per-field entries.
- **Claim** — one published assertion a reader sees. In palm_tier2 that is one field
  row of a facility dossier, e.g. "cpo_production = 52,228 MT, high confidence,
  primary source". A claim lives *inside a payload* of a publish-stage input row —
  it is finer-grained than any row.
- **The LLM** — always spelled out; "model" is avoided in this doc because it also
  means data model. `llm_transform` stages call an LLM once per input row.
- **Hop** — one stage-to-parent step in a trace.

## 1. Problem

A published dossier asserts claims with a bare source link. A reader cannot see how a
value survived the pipeline: what competed with it, which document each candidate came
from, where each column of the final row was written, and which steps were mechanical
versus LLM assertions. The run's persisted outputs contain the rows to answer this,
but nothing records how rows connect across stages, and nothing renders the chain.

## 2. Decisions settled in discussion (2026-07-01/02)

1. **Audience: both, reader-facing first.** One trace record serves a reader-facing
   renderer (built now) and an author-facing debugger (deferred; a deeper display over
   the same record, not a second data shape).
2. **Rows-in / rows-out, no narrative layer.** For each hop, show the stage's input
   rows and output rows verbatim. LLM fan-in stages have no formal "pick a row"
   operation, so the renderer must not invent selection semantics ("chosen",
   "rejected") — an earlier mockup did exactly that and was discarded for it.
3. **The runtime tracks lineage, not the tracer.** Lineage — which input rows produced
   which output rows — is recorded by the runtime *while it executes each stage*,
   from the execution structure it already controls. It is never reconstructed
   afterwards from an understanding of what a transform does internally, and never
   derived from values an LLM wrote (a URL string an LLM emitted is untrusted display
   material, not lineage). This decision supersedes the first draft's key-declaration
   walk.

## 3. What the runtime does today (inventory, no changes yet)

Verified against `app/runtime/handlers.py` on this branch, plus open PR #26
(`python-row-frame-functions`), which this design assumes lands first:

| Stage type | How the runtime executes it | Input→output row mapping visible to the runtime during execution? |
|---|---|---|
| `input_data` | reads a file into a table | trivially — each row originates here |
| `python_row_function` (PR #26) | runtime maps the function over the single input's rows: one dict in, one dict out; the function never sees the whole table | yes — 1:1 by position, by construction |
| `python_frame_function` (PR #26) | calls the function with whole input table(s); it may reshape freely (group, dedup, filter, explode) | **no — opaque.** The only stage type where the runtime cannot see the mapping |
| `llm_transform` | runtime iterates input rows, one LLM call per row; a dict result becomes one output row, a list result becomes N output rows, each merged onto its input row's columns | yes — 1→N per input row, exact; the runtime also knows *which columns* the LLM wrote (the result dict's keys) vs carried from the input row |
| `join` | runtime itself merges exactly two inputs on keys declared in the stage's `join:` config (the one place "join keys" is a formal, enforced concept — the handler raises without them) | yes — via the merge |
| `aggregate` | runtime itself groups the input on the stage's declared `group_by` columns | yes — each output row ← its group's member rows |
| `human_review_queue` | keyed edits in place, matched by content hash | yes — 1:1 |
| `publish` | consumes input rows, writes artifacts | yes — row → artifact |

**None of this is recorded anywhere.** The mapping exists transiently in handler
local variables and is discarded. That is the gap this design fills. Note what is
*absent* from the table: nothing pipeline-wide called a "join key" or "lineage key"
exists today, and `schema.primary_key` declarations on stages are documentation-grade
(validated, but nothing derives lineage from them).

## 4. Lineage tracking (the new runtime capability — the core of this design)

### 4.1 Recorded edges

Each handler (or the runner wrapping handlers uniformly) records, for every output
row, the input row(s) that produced it, per the table above. Persisted per run as
`runs/<run_id>/lineage/<stage_id>.parquet`:

| column | meaning |
|---|---|
| `out_row` | ordinal of the output row in this stage's persisted output |
| `in_stage` | parent stage id |
| `in_row` | ordinal of the input row in that parent's persisted output |

One row per edge; a fan-in output row has many edges, a fan-out input row appears in
many edges. For `llm_transform` stages the runtime additionally records
`llm_columns/<stage_id>.json`: the list of output columns whose values the LLM wrote
(the union of result-dict keys), versus columns carried from the input row. This is
observed at execution time, not declared.

Edges recorded this way are labeled `recorded` in the trace: the runtime witnessed
the mapping.

### 4.2 Recovered edges (the frame-function fallback)

`python_frame_function` stages are opaque, so nothing can be recorded. Rather than a
hard hole, the tracer may attempt **recovery** after the fact: if the stage's declared
output primary-key columns also exist on the input, and each output row's key values
match exactly one input row, that mapping is emitted with label `recovered` — a
weaker claim than `recorded`, and every recovered edge is mechanically verifiable
(exact value equality on named columns). If recovery fails (key columns absent,
ambiguous matches, key values not found upstream), the hop is flagged `untracked` and
the trace says so plainly. Recovery is a display aid, never silently blended with
recorded edges.

The lasting fix is re-typing: several palm_tier2 stages currently compiled as generic
python transforms are structurally narrower — e.g. `collate` (one row per facility,
packing that facility's per-document rows into a payload) is a group-by-and-pack, i.e.
an `aggregate`, whose lineage would then be recorded, not recovered. Re-typing is
follow-up work per stage, not part of this feature.

### 4.3 What lineage is *not* derived from

- Not from `schema.primary_key` declarations (documentation-grade today).
- Not from understanding transform internals (no per-stage "payload path" or "key
  relationship" declarations — the first draft's mechanism, dropped).
- Not from values an LLM wrote. Example of the excluded case: in palm_tier2, the
  adjudicate stage's output carries a `source_url` column the LLM filled in to name
  the document it drew each field's value from. That string is useful *display*
  material (§6) but is never treated as lineage — the LLM could emit a URL matching
  nothing, or matching the wrong row.

## 5. The trace: a walk over recorded edges

A trace starts from a claim. The claim's *row* is found by construction at publish
time (the publish stage is iterating that exact input row when it renders the claim);
the claim's position *inside* the row's payload is view information (§6), not lineage.

From that row, follow lineage edges backward, transitively, to `input_data` stages:
the result is a per-claim subgraph of rows — every row in every stage that is an
ancestor of the published row. Each hop of the rendered trace is: one stage, its
ancestor rows in this subgraph ("rows-out"), and for each parent stage the ancestor
rows there ("rows-in"), with edge labels (`recorded` / `recovered`) and hop flags.

Because lineage is row-grained, the subgraph naturally *widens* at fan-ins: in
palm_tier2, a facility's adjudicate row traces back through collate to all ~10 of the
facility's per-document extract rows — not only the documents that mention the
claim's field. Narrowing the display to the relevant payload entries is the view
layer's job (§6); the lineage layer never filters.

## 6. The view layer (rendering; may read payloads, clearly demarcated)

### 6.1 How a user sees a trace

Three rules, in order of decision weight:

1. **Lineage is stored in sidecar files, never as columns on the data tables.**
   `lineage/<stage_id>.parquet` sits next to `outputs/<stage_id>.parquet` in the run
   directory; a stage's output table is byte-identical to what it would be without
   this feature. (Burning edges onto tables wouldn't even fit — a fan-in output row
   has many edges, i.e. lineage is a separate relation, not extra columns.)
2. **Traces are computed on demand, keyed by `(run_id, stage_id, row ordinal)`.**
   A trace is a cheap graph walk over the sidecars plus row lookups in persisted
   outputs — nothing is precomputed or stored per trace. The primary surface is the
   run viewer: the existing per-stage table views gain a per-row "show your work"
   action at `run/<run_id>/<stage_id>/<row>`, rendering the hop cards of §5. A
   claim's sub-row position (its field inside the payload) travels as a query
   parameter and drives payload narrowing (§6.2, item 2) only — the walk itself is
   row-grain.
3. **The published dossier is a static client of the same tracer.** A reader of the
   HTML artifact has no server, so the publish stage calls the same trace function
   the viewer route calls, inlines the (capped, elided) rendering per claim, and
   writes the complete trace JSON alongside as the raw companion
   (`artifacts/palm_tier2/traces/<facility_id>.json`). One tracer, two callers —
   never a second lineage mechanism for the static case.

### 6.2 Derived annotations

The renderer consumes (a) the lineage subgraph, (b) the persisted stage outputs, and
(c) the recorded LLM-column lists. On top of verbatim rows it adds only these derived
annotations — this list is exhaustive; anything else is scope creep to be rejected:

1. **Column badges.** From recorded data only: a column an LLM wrote at this stage
   (`llm_columns`), a column carried from the input row, a column originating at an
   `input_data` stage.
2. **Payload narrowing** (display refinement, lineage untouched): within rows-in/
   rows-out, highlight or filter payload entries matching the claim — e.g. show, of a
   document's extracted fields, the entry for the claim's field. Requires knowing
   which payload column holds entries and which entry key names the field; this pair
   is a *view* declaration, example-local, and its absence only means no narrowing.
3. **Reference matching** (display only): for an LLM-written column that names
   another row — the palm adjudicate `source_url` example above — annotate exact
   string equality against rows-in ("equals row 1's `url`"), or the explicit warning
   "matches no input row". Exact match only; never fuzzy, never normalized, never
   lineage.
4. **Counts.** N rows in → M rows out per hop; "showing K of N" when display caps.

### 6.3 The two renderings

**Run-viewer view (primary surface):** hop cards at `run/<run_id>/<stage_id>/<row>`,
newest hop first, computed on request per §6.1. Deterministic 1:1 hops collapsed by
default; URLs shortened; huge cells elided with markers, expandable since the server
has the run dir. This is also where the deferred author-depth features (full column
sets, every hop expanded) later attach — same route, deeper display.

**Dossier view (static):** each claim row in the published HTML expands to the same
hop cards, inlined at publish time per §6.1 rule 3, with the trace JSON companion
carrying anything the inlined view capped or elided.

## 7. Corner cases and decided behavior

- **C1 — LLM-written reference matches nothing** (e.g. adjudicate emits a
  `source_url` equal to no input row's `url`, or a value present in no candidate):
  the view shows the explicit "matches no input row" warning. A feature, not an
  error — it is the strongest red flag the trace can raise. Lineage is unaffected
  (the row's ancestry is recorded regardless).
- **C2 — claim's field supported by no upstream payload entry** (the adjudicator
  invented a field): lineage still traces the row normally; payload narrowing (§6.2)
  finds zero matching entries upstream and the view flags it. The first draft broke
  the whole chain here; with row-grained lineage nothing breaks — the anomaly is
  visible *and* the ancestry remains inspectable.
- **C3 — output row with no recorded or recovered edges** (frame-function hole, or a
  handler bug): hop flagged `untracked`; the trace ends there explicitly and says
  why. Never silently continue by guessing.
- **C4 — sub-row linkage that was never materialized**: lineage says *which input
  row* produced an output row, not which part of it. Example: in palm_tier2, locate
  is an `llm_transform` fanning one facility row out to ~10 document rows; recorded
  lineage ties each document row to its facility row, but *which suggested query*
  (an entry inside the facility row's `queries_json` payload) surfaced each document
  was never output by the stage. The view states this ("query→document linkage not
  recorded by the pipeline") rather than implying precision that doesn't exist.
- **C5 — LLM-written values used as identifiers downstream** (locate's LLM writes
  `url`; every later stage keys on it): downstream lineage is recorded execution
  structure, so it is sound regardless; the column badge shows `url` was LLM-written
  at locate, i.e. the identifier's *origin* is an LLM assertion. WebSearch results
  behind it are not captured (non-goal).
- **C6 — malformed / null payload where the view expects entries**: the view flags
  `unparseable_payload` and preserves the raw cell; never coerce to "no entries".
  (Existing smell, to fix when touched: `examples/palm_tier2/code/publish_tier2.py`
  `_coerce` silently returns `[]` on `JSONDecodeError`.)
- **C7 — duplicate rows where the stage schema declares uniqueness**: the trace is a
  witness, not an enforcer — show all rows, flag `pk_violation`.
- **C8 — multi-parent stages** (palm's `capacity_crosscheck` joins two parents):
  edges carry `in_stage`, so a hop renders one rows-in table per parent. Recorded
  via the join handler's own merge.
- **C9 — frame-function stages on the path** (palm today: collate, trase_unique,
  select_for_enrichment, flatten_facilities, and the fetch/parse/grep trio until
  re-typed as row functions under PR #26): recovery per §4.2, else `untracked`.
  This is the design's honest weak spot, and the pressure it creates — re-type
  stages to structurally narrower types — is intended.
- **C10 — large row sets at a hop**: the JSON record holds all rows (bounded by the
  run); the reader view caps at K with explicit "showing K of N". No silent
  truncation anywhere.
- **C11 — huge cells** (palm's `doc_text` reaches hundreds of KB): elided in the
  record with an explicit marker — column name, char count, pointer to the source
  parquet — never dropped without a marker.
- **C12 — partial runs / missing stage output or lineage files**: hop flagged
  `missing_output_file`, trace ends there explicitly.
- **C13 — value-format variance across sources** (`52,228` vs `678475.00`): rendered
  verbatim; no display normalization; reference matching stays exact-string. If
  normalization is ever wanted it is a pipeline stage, visible as its own hop.
- **C14 — legitimately empty upstream payloads** (most `grep_fields` snippet cells
  in the 2026-06-29 palm run are `{}`): empty is data, shown as-is; the reader view
  may collapse such hops by default (display depth only).
- **C15 — "why is this facility in the report at all"**: selection provenance is the
  same row-lineage walk entered from a facility row instead of a claim; no reader
  entry point in v1.

## 8. Relationship to open work

- **PR #26 (`python-row-frame-functions`) is the substrate.** Its row/frame split is
  what makes most python stages' lineage recordable rather than recovered. Lineage
  recording touches the same handlers, so the runtime part of this work should stack
  on that branch, not on master directly.
- **PR #18 (eval data model, merged)** introduced `is_grain_preserving` — a veto on
  where declarative evals may tap. Runtime-recorded lineage is strictly stronger
  information; a later follow-up could derive the veto (and more) from lineage
  instead of stage-type claims. Nothing here blocks on that.
- **This worktree's base (`palm-on-master`) is a local not-for-merge branch.** Fine
  for prototyping the renderer against real palm runs; the runtime lineage changes
  must be cut as a clean PR on top of PR #26's branch. Split decided at
  implementation planning.

## 9. Testing

- Unit tests per handler: recorded edges match hand-computed expectations on small
  fixtures (fan-out LLM with list results, join, aggregate, row function 1:1).
  Offline, mock LLM backend, matching the existing test convention.
- Recovery tests (§4.2): unambiguous match, ambiguous match (→ `untracked`), key
  columns missing (→ `untracked`).
- Corner-case tests constructible offline: C1, C2, C3, C6, C7, C8, C10, C11, C12, C13.
- A render smoke test asserting the derived annotations (§6) against hand-computed
  values for a fixture run.
- Real palm runs (machine-local, gitignored) are manual acceptance checks only.

## 10. Non-goals (v1)

- Capturing LLM prompts/responses per row (the author view will want it later;
  runtime change, separate).
- Span-level evidence (page/offset highlights inside source PDFs) — an extraction
  improvement, independent.
- Fuzzy or semantic matching anywhere in trace or view.
- The author-facing viewer UI.
- Re-typing palm stages (collate→aggregate etc.) beyond what the SYW path minimally
  needs — each re-type is its own small change.
- Fixing the loose document scoping found during design (a different mill's audit in
  a facility's document set — separate lead, task chip filed).
