# Named schemas (data-model-first) — build learnings

Built on branch `named-schemas` (off PR #3). Goal: make "named schemas" a
first-class concept separate from the DAG, and author the lobbymap data model as
named schemas to present. This note is what I learned doing it.

## What was built

- **`app/dag_schema.py`** (the contract) — added `SCHEMA_KINDS`, `validate_named_schema`,
  `validate_schema_library`. A named schema = a `TableSchema` + `name` + `kind`
  + per-column `references` (explicit FK to another schema). Pure functions, no
  runtime/compiler imports (keeps the interface clean).
- **`app/main.py`** — `load_schemas()` (multi-doc YAML), `build_schema_er_diagram()`
  (FK edges from explicit `references`, not PK-name collisions), a
  `/methodology/{m}/schemas` route, and `list_methodologies()` now recognizes a
  methodology that has a data model but **no DAG** (the whole point).
- **`app/templates/schema_library.html`** — renders the data model grouped by
  kind, with validation status + ER diagram.
- **`examples/lobbymap/schemas/*.yaml`** — 16 named schemas (the lobbymap data
  model), validating CLEAN.

## Core finding: the prototype was DAG-first; this inverts it

`data_model.html` derives the data model *from* the compiled DAG — it renders each
stage's `output_schema`. So you could not author a data model without first
authoring the DAG. Named schemas make the data model the primary artifact; the
DAG (step 2) wires transforms between named schemas. lobbymap proves *why* you
need this order: you cannot write the scoring stage until `query`, `data_source`,
and `benchmark` exist as tables — and nothing *produces* those; they're reference
data. A DAG-first tool has nowhere to put them.

The named-schema view also expresses things the DAG-derived view structurally
cannot:
- **`kind`** (reference | input | computed | ground_truth) — the DAG can't say
  `benchmark` and `cell_weights` are "reference data we must source, not compute."
- **Explicit FK graph** — `references` instead of guessing FKs from PK-name
  collisions.
- **NA/NS modeling** — `cell_score` splits `status {scored,NA,NS}` from a nullable
  `score`, so absence ≠ zero (the exact bug congresswatch FINDINGS flagged).
- **Ground-truth tables** for the (still-unbuilt) eval.

## Reconciliation: lobbymap ALREADY had a 14-stage DAG

`examples/lobbymap/` pre-existed in PR #3 with a full compiled DAG (I'd missed it;
my first tree listing was truncated mid-congresswatch). So this is now a real
before/after on one methodology. Diffing my web-research data model against the
existing DAG's stage output_schemas surfaced three things my model got wrong or
missed — the DAG was a better source than the web on these:

1. **`cell_weights` exists as a curated input** keyed `(sector, source_class,
   query_id)→weight`. The "proprietary aggregation weights" I'd called undisclosed
   have a *modeled home* as `computed_static` reference data. My model omitted this
   table entirely — a real bug, now fixed (added as a `reference` schema).
2. **Entity unification** — the DAG's `tracked_entities` merges companies +
   associations into one table with `entity_kind`, so the matrix keys on a single
   `entity_id`. I kept `company` / `association` / `company_association_link`
   separate. Theirs is a cleaner matrix spine; mine keeps the funding-edge explicit
   (which the conduct-layer extension needs). **Open design fork — for review.**
3. **Benchmarks are richer** — `precedence_rule` + `jurisdiction`, many-per-query,
   resolved by precedence (the `evidence_with_benchmarks` join is keyed
   `(evidence_id, benchmark_id)`). And evidence carries `importance` /
   `recency_weight` / `composite_weight` (the `importance_tagging` stage). My
   benchmark/evidence schemas flattened both. Noted, not yet folded in.

Also: the existing universe is **Forbes Global 2000** (`forbes_global_2000`), not
"oil & gas." Oil & gas is our eval *slice/filter*, not the methodology's universe.

The existing DAG has **no ground-truth / eval tables** — consistent with
"Eval (planned)." My `gt_*` schemas are net-new and the right addition.

## Open design questions for review

- **Entity model:** unify (their `entity_kind`) vs. keep separate company/
  association + link (mine). The conduct-layer extension wants the explicit edge;
  the matrix wants unification. Possibly both: unified `entity` for the matrix +
  a `company_association_link` edge for funding.
- **`source_class` enum vs. `data_source` dimension table** — they denormalize; I
  normalized. Normalized is cleaner but diverges from the existing DAG.
- **Fold in** benchmark precedence/jurisdiction + evidence importance/recency
  weighting, or keep the data model lean and push those into stage logic?
- **Step 2:** should stages reference named schemas by `schema_ref`, and should the
  existing 14 lobbymap stages be refactored to consume/produce these named schemas
  (making the library the single source of truth instead of 14 duplicated
  output_schema declarations)?
