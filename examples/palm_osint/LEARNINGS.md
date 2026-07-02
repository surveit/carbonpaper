# palm_osint — what the build taught us

Built 2026-06-17 in a throwaway worktree (`prototype_one_palm_wt`, branch
`palm-osint-dag`). Goal: re-express the whole `food-bev-facility-osint` palm
pipeline as a prototype_one DAG and run it for 5 facilities. It runs: 12 stages,
all 7 node types, halt-for-review, resume, 5 published dossiers, **zero
fabricated positives**.

Epistemic tags below: *Observed* = happened in the run. *Inference* = my read.

---

## What the run actually did (Observed)
- `python -m app.runtime.runner examples/palm_osint` → halts at `enrichment_review`
  (`awaiting_review`, 5 pending). Inject 5 approve decisions → `resume_run` →
  `status: ok`, 12/12 stages green, 5 HTML dossiers + index.
- Final asset: 5 `documented_negative` (methane capture: "no project found"),
  10 `unknown_gap`. No `present=true` survived — the honest outcome, because the
  offline mock never asserts a feature without a source.
- `coverage` (aggregate node) reproduced the real gap: Indonesia 1478 mills,
  95% with structured capacity, 83% multi-source; every other country 0%
  capacity (Trase is Indonesia-only). Matches the pipeline's `coverage.json`.

## What worked — and validates the merge thesis
1. **The node types genuinely fit.** All 11 conceptual steps from §2 of the
   thinking doc landed on a real node type without contortion. The DAG reads as
   the pipeline.
2. **Validation earned its keep on the FIRST run (Observed).** The `join` fanned
   one facility into a duplicate `facility_id` (2327 from 2326) because a UML id
   appears on two Trase rows. The PK-uniqueness check flagged it immediately —
   a raw Prefect join task ships that silently. This is exactly the RETHINK
   "caught real-data quirks immediately" claim, reproduced. Fix: a `trase_unique`
   dedup stage.
3. **The halt-for-review gate is free and real (Observed).** Tier-2 today has no
   human-review primitive; here the run stopped, persisted a queue snapshot,
   and `(facility_id, feature)` content-hashing made the decisions durable across
   the resume. This is the ⚑ gain the doc predicted.
4. **The anti-fabrication discipline is enforceable at the seam (Observed).**
   extract → adversarial-verify → drop-refuted/demote-unverified ran as typed
   stages. A `present=true` with no supporting verdict is dropped in
   `apply_verdicts`, never shown as fact.

## What didn't / the friction (the honest costs)
1. **`python_transform` validates I/O, not internals (DA#1, confirmed).** The
   flatten + select + apply-verdicts logic is arbitrary Python. The typed
   contracts around it are checked; the logic inside is not. Real gain over a
   raw task, but not "no more untested Python." Name it correctly.
2. **The tabular substrate is lossy on nested provenance (DA#3, confirmed — the
   deepest cost).** I chose to FLATTEN: scalar provenance that matters
   (`capacity_value/unit/source_url/provenance`) is promoted to typed columns so
   `validation.py` can range/null-check it. Variable-length lists (`aliases`,
   `contributing_sources`, `reported_emissions`) survive only as JSON-encoded
   columns the runtime treats as opaque. So validation sees the *one* capacity
   number but is blind inside the source lists. For a multi-source provenance
   asset that's a standing cost, not a one-time adapter.
3. **Connectors are still the genuine ⚠ (DA#4).** I added a `geojson` file-format
   reader, but that's a file lift, not the network `scrape`/`http` lift. Tier-1
   ran off committed dated snapshots (the §4 Step-4 fallback) because the live
   PalmWatch CSV is uncommitted and its origin 522s. Real connectors remain
   unbuilt.
4. **Incremental skip not addressed (DA#2).** This run re-touches every Tier-2
   row every run. The content-hash exists (the queue uses it); extending it to
   skip unchanged `llm_transform` rows is still TODO before Tier-2 is affordable
   at scale.
5. **Small papercuts.** YAML parses bare `true/false` as booleans, so a
   `range: [true,false,unknown]` enum silently mismatched the string data until
   quoted. The `llm_transform` handler injects an `evidence_id` column that
   shows as an undeclared-column warning.

## Runtime changes made (general, not palm-specific)
- 🔵 `app/runtime/handlers.py` — `format: geojson` for the `file` connector.
- 🔵 `app/runtime/runner.py` — generic `limit:` on any stage (caps output rows).
- 🔵 `app/runtime/llm_mock.py` — honest palm Tier-2 mock: never asserts
  `present=true`, never invents a URL or number.

## What is mock vs real (honesty)
- **Real, committed data:** Trase IDN GeoJSON (1452), the resolved
  `facilities.jsonl` (2326), capacity values + provenance, owners, locations.
- **Mock:** only the Tier-2 *feature extraction/verification*, and only because
  `claude` isn't on PATH here. The mock is truthful (declines to assert). Put
  `claude` on PATH (it's at `C:\Users\shuha\.local\bin`) and the same DAG runs
  real `claude -p` haiku — the prompts already carry the anti-fabrication rules.

## Inference / recommendation
The merge is the right call and the wedge order in §4 holds. The thing to decide
deliberately before building more is the substrate (DA#3): the flatten works and
is honest, but if the asset's value is multi-source provenance, validating
*inside* nested schemas (vs. flattening) may be worth the runtime extension. This
build is the cheapest possible test of that fork — and it says: flatten is
shippable today, nested-validation is the upgrade.
