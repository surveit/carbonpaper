# LobbyMap eval — reproducing InfluenceMap

The flagship example. Goal: implement InfluenceMap's **LobbyMap** methodology
closely enough to build an eval dataset from their *published* results and test our
pipeline against it. Everything lives in `examples/lobbymap/`.

Deep dives (read these for detail; this doc is the map):
- `examples/lobbymap/RESEARCH.md` — InfluenceMap's actual methodology + data
  availability, with provenance.
- `examples/lobbymap/LEARNINGS.md` — what building the named-schema data model
  taught us (incl. reconciliation against the pre-existing 14-stage DAG).
- `examples/lobbymap/LEVEL2.md` — the first end-to-end run + how to extend it.

## What InfluenceMap's LobbyMap is (one paragraph)

It scores corporate climate-policy engagement on a **matrix of (policy query × data
source) cells**, each scored **−2..+2** vs a science-based **benchmark** (IPCC/IEA
derived — this is what makes it "policy neutral"). Per-cell scores roll up to an
Organization Score (0–100), a Performance Band (A+…F), Engagement Intensity, and a
Relationship Score (via industry associations). Companies AND "influencers"
(industry associations) are scored the same way.

## What we model (and deliberately don't)

The generation data model (`schemas/`) stops **at the matrix**: `query`,
`data_source`, `benchmark`, `company`, `influencer`, `document`, `scored_evidence`,
`cell_score`. Decisions baked in (see LEARNINGS):
- **Stop at the matrix.** The Organization/Relationship/Band rollups are out of
  scope — their aggregation is a *proprietary, undisclosed* algorithm we can't
  faithfully reproduce. The matrix is the artifact worth building/evaluating.
- **Companies and influencers are separate tables** (mirroring InfluenceMap's CMS
  `/company/` vs `/influencer/`). The scored entity is company **XOR** influencer
  via an `exclusive_arc` on `scored_evidence` and `cell_score`.
- **`document` carries no entity attribution** — that's an analytical claim, made at
  `scored_evidence` (the scored + sourced + reviewable unit; `cell_score` is its
  aggregate).

## The eval, and the key constraint

**We do NOT crawl lobbymap.org.** Their `robots.txt` disallows `/evidence/` and
`/score/` and bans automated agents — and they're a fellow public-interest org. So:

- **Ground truth** is ingested from **manually-saved** cell pages: a human saves a
  LobbyMap cell page to `~/Downloads`, and `eval/ingest_html.py` parses it locally
  into `eval/ground_truth/{raw_cells,gt_scored_evidence,gt_cell_score}.jsonl`.
- The eval data model (`eval/*.eval.yaml`) derives ground-truth schemas from the
  generation schemas (see [named-schemas.md](named-schemas.md)).

### Level-2 reproduction (the run)

Three isolation levels exist; we run **Level 2**: borrow InfluenceMap's OWN cited
source documents (fetched from the *original publishers*, not lobbymap), run our
extraction + scoring, compare to their published scores. This controls for document
*discovery* (the hardest, least reproducible part) and tests extraction + scoring —
the only interpretable v1. `run_level2.py` does this; `render_report.py` makes an
HTML report.

**Status:** the first cell (CalPERS, Q2 "Climate Science Stance" / D2 "Corporate
Media") reproduced exactly — our evidence `[2,2,2]`, cell `+2`; theirs `[2,2,2]`,
cell `+2`.

**Honest caveats (do not over-read the result):** it's an *easy* cell (unambiguous
pro-climate investor statements both sides score +2); the benchmark is
*reconstructed* from the query definition, not InfluenceMap's actual Benchmarks DB;
and it's **N=1**. The eval only becomes meaningful with **contested** cells.

## How to extend (the next agent's job)

1. In a browser, open more LobbyMap cells (any entity × query × source) and **save
   the page** into `~/Downloads`. **Prioritize CONTESTED cells** (oil & gas majors;
   scores of −2/−1/0) — easy +2 cells tell us little.
2. `python examples/lobbymap/eval/ingest_html.py ~/Downloads` → updates ground truth.
3. Add the cited source docs to the corpus (`data/build_corpus.py`) and generalize
   `run_level2.py` to loop over all ingested cells (currently hardcoded to one).
4. For scale beyond hand-saved cells: the right path is to **request sanctioned data
   access from InfluenceMap** (they're a data provider to Climate Action 100+).

## Note on the legacy DAG

`examples/lobbymap/compiled/` still holds an older **14-stage DAG** (the original
DAG-first build). The named-schema model is a redesign that supersedes its data
modeling; the two were reconciled (see LEARNINGS — it's where `cell_weights`, the
entity-unification question, and the schema-drift bugs came from). Don't assume the
14-stage DAG matches the `schemas/` model.
