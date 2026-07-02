# Level-2 run — status & how to extend

First end-to-end run of the lobbymap methodology on real InfluenceMap ground
truth. Built while you were away from the one cell you downloaded.

## What ran
**Cell: CalPERS / Q2 (Climate Science Stance) / D2 (Corporate Media).**
Level-2 = borrow InfluenceMap's own cited source documents, run OUR extraction +
scoring, compare to their published scores. Isolates extraction+scoring from
document discovery (the only interpretable v1).

Pipeline: `document → [llm: extract+score] scored_evidence → [aggregate] cell_score → eval vs gt`.

## Result
| | our pipeline | InfluenceMap | match |
|---|---|---|---|
| evidence scores | [2, 2, 2] | [2, 2, 2] | ✓ |
| cell score | **+2** | **+2** | ✓ (abs err 0) |

Our pipeline independently scored all three Investor Statements +2 and reproduced
their cell exactly. Report: `runs/level2_calpers_q2_d2/report.html`.

## Honest caveats (read before trusting this)
- **Easy cell.** These are unambiguous pro-climate investor statements both sides
  scored +2. This proves the pipeline RUNS and reproduces a clear case — NOT that
  it holds on contested cells (scores near 0, oppositional oil majors, quoted-
  opposition stance-flips). The eval only gets meaningful with hard cells.
- **Reconstructed benchmark.** We scored against a benchmark rebuilt from the query
  definition + the public -2..+2 scale, NOT InfluenceMap's Science-Based Benchmarks
  DB (which we don't have). On borderline cells, disagreement would be partly
  benchmark drift, not methodology error.
- **N=1 cell.** Agreement on one cell is anecdote, not evaluation.

## How to extend (add more cells)
1. In a browser, open more LobbyMap cells (any entity × query × source) and **save
   the page** (`File > Save`) into `~/Downloads`. We parse saved pages — we do NOT
   crawl lobbymap.org (robots.txt disallows /evidence/ + /score/ and bans bots).
2. `python examples/lobbymap/eval/ingest_html.py ~/Downloads`
   → updates `eval/ground_truth/{raw_cells,gt_scored_evidence,gt_cell_score}.jsonl`.
3. For each new cell, add its cited source docs to the corpus (the original-
   publisher URLs are in `gt_scored_evidence.jsonl`), then extend `run_level2.py`
   to loop cells. (Currently hardcoded to the CalPERS cell — generalize when there
   are >1.)

**Prioritize saving CONTESTED cells** (oil & gas majors, scores of -2/-1/0) — those
are where reproduction is actually tested. The easy +2 cells tell us little.

## Files
- `data/build_corpus.py` · `data/documents.jsonl` — source PDFs → document corpus
- `data/source_pdfs/` — the 3 fetched Investor Agenda PDFs (original publisher)
- `eval/ingest_html.py` · `eval/ground_truth/` — saved-page → ground truth
- `eval/*.eval.yaml` — eval specs (gt derived from generation schemas)
- `run_level2.py` · `runs/level2_calpers_q2_d2/` — the run + report

## ✅ CHECKLIST FOR HUMAN
- [ ] Skim `runs/level2_calpers_q2_d2/report.html` — confirm the extracted quotes look right.
- [ ] Save 3–5 CONTESTED cells (oil major, low/mixed scores) to ~/Downloads so the next run tests the hard path, not just an easy +2.
- [ ] Decide whether to email InfluenceMap for sanctioned data access (needed for scale beyond hand-saved cells).
