# Palm-oil facility OSINT — as a prototype_one methodology

This methodology re-expresses the `food-bev-facility-osint` palm-oil pipeline
(today: generic Python under Prefect) as a prototype_one DAG, per
`docs/osint_integration_thinking.md`. It folds the whole flow — structured-source
ingest → entity resolution → field merge → coverage → Tier-2 LLM enrichment with
adversarial verification → human review → published asset — into the seven typed
node types, so every inter-stage boundary is schema-validated and the expensive
LLM step sits behind a halt-for-review gate.

## §1 Sources (input_data)
Two committed structured snapshots stand in for the live fetch (live PalmWatch
CSV is uncommitted and its origin 522s; live Trase is a bulk GeoJSON). This is
the plan's Step-4 fallback: a dated snapshot feeds a `file` connector.
- **Trase Indonesia mills** GeoJSON — the only structured nameplate-capacity source.
- **Facilities spine** — the committed `facilities.jsonl` (the resolved Tier-1
  asset), standing in for the PalmWatch global mills CSV + prior resolution.

## §2 Flatten to the tabular substrate (python_transform)
The nested `FacilityRecord` (Quantity / SourceRef / Location / variable-length
lists) is flattened to typed columns. Scalar provenance that an auditable asset
depends on — capacity value, unit, source URL, provenance class — is promoted to
first-class columns so `validation.py` can see it. Variable-length lists are kept
as JSON-encoded columns (lossy-but-preserved). This is the load-bearing substrate
decision (Devil's-Advocate #3): flatten, don't pass an opaque blob.

## §3 Capacity cross-check (join)
Left-join the flat facilities to the live Trase mills on `uml_id` to surface the
structured capacity alongside each facility — a multi-source corroboration check.

## §4 Coverage (aggregate)
Per-country: facility count, % with structured capacity, % multi-source. Mirrors
the pipeline's `coverage.json`.

## §5 Select for Tier-2 (python_transform, limited)
Order facilities so the most informative (Indonesian, capacity-bearing,
multi-source) come first, then the runtime `limit:` caps the set for a dry run.
The cap is a demo throttle; in production the re-enrichment queue (changed/new
facilities) feeds this instead.

## §6 Tier-2 extract (llm_transform)
Per facility, an LLM proposes candidate on-site features (methane capture, biomass
boiler, RSPO status, …). Hard rule: no sourced URL ⇒ `present = unknown`; never
invent an evidence URL or a number. Emits one row per candidate feature.

## §7 Tier-2 adversarial verify (llm_transform)
A second pass tries to REFUTE each feature claim by re-checking its cited source.
Default = refuted: a claim with no verifiable source is not supported.

## §8 Confirm before publish (human_review_queue)
Every feature on which the model took a definite stance (`present != unknown`)
halts the run for a human to confirm or reject before it can become asset.
Hash on `(facility_id, feature)` so decisions survive re-runs.

## §9 Apply verdicts (python_transform)
Drop refuted positives, keep verified, keep documented negatives, flag unknown
gaps. A `present=true` feature with no supporting verdict is dropped — never
silently shown as fact.

## §10 Publish dossiers (publish)
Render per-facility HTML dossiers + an index, reconstructing capacity provenance
and the verified/negative/gap feature findings from the flat columns.
