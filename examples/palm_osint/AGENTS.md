# examples/palm_osint — the full palm-oil OSINT flow as a DAG

The first specimen: the entire `food-bev-facility-osint` palm pipeline (Tier-1
facility resolution + Tier-2 LLM enrichment) re-expressed as a **12-stage DAG
exercising all 7 node types**, with schema validation between stages and a
halt-for-review gate. Built per `docs/osint_integration_thinking.md` (in the
food-bev repo). The deeper write-up is `LEARNINGS.md`.

## The flow
```
input_data(trase geojson) ─┐
input_data(facilities.jsonl spine) → flatten_facilities → capacity_crosscheck(join)
   → coverage(aggregate)                                      │
   → select_for_enrichment (limit: 5)                         │
   → tier2_extract(llm) → tier2_verify(llm, adversarial)
   → enrichment_review(human_review_queue ← HALTS)
   → apply_verdicts → publish_facility_dossiers
```
- **Substrate decision (the load-bearing call):** `flatten_facilities` turns the
  nested `FacilityRecord` (Quantity/SourceRef/lists) into flat typed columns so
  `validation.py` can see the provenance (capacity value/unit/source-url). Lists
  survive as JSON columns (lossy-but-preserved). See `LEARNINGS.md` §"What didn't".
- **Tier-1 source is a committed snapshot** (`data/facilities.jsonl`, the resolved
  asset) + the real Trase geojson — the plan's Step-4 fallback, since live PalmWatch
  fetch is uncommitted/flaky. `capacity_crosscheck` is a `join` against live Trase.
- **Tier-2** = extract candidate on-site features → adversarially verify → human
  confirm → drop-refuted/demote-unverified → publish. Never asserts a feature
  without a source.

## Run it
```
CW_LLM_FORCE_MOCK=1 python -m app.runtime.runner examples/palm_osint   # deterministic, offline
python -m app.runtime.runner examples/palm_osint                       # agent_sdk (real model)
```
It halts at `enrichment_review` (status `awaiting_review`). Record decisions
(`decisions/enrichment_review.parquet`, content-hashed) then
`resume_run(...)` (or the web UI's Resume) to finish. The offline mock
(`app/runtime/llm_mock.py`) is honest: it declines to assert features / never
invents a URL, so a mock run yields documented-negatives + gaps, zero fabrication.

## Layout
- `compiled/*.yaml` — the 12 stages (each column carries a `source:` provenance badge).
- `code/` — the `python_transform` modules (flatten, select, apply_verdicts, publish).
- `data/` — committed snapshots (facilities.jsonl, Trase geojson, a PalmGHG sample).
- `scripts/tier2_inspect.py` — **the Tier-2 LLM inspector REPL**: render the exact
  prompt, watch the agent think/search live, toggle `--tools` (WebSearch/WebFetch),
  switch model, edit prompts, iterate on one facility. `python -m
  examples.palm_osint.scripts.tier2_inspect --facility <id> --tools`.
- `research_runs/` — **3 unstructured Claude Code research transcripts** (the raw
  material) + `DISTILLATION.md` (the hand-distillation of them into a pipeline).
  These are what `examples/palm_tier2` was distilled from.
- `LEARNINGS.md` — what worked / what didn't from building this.
- `runs/`, `decisions/` are gitignored.
