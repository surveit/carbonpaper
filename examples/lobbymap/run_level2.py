"""
Level-2 run: given InfluenceMap's OWN cited source documents, can our pipeline
reproduce their evidence scoring? This isolates extraction + scoring from document
discovery (we borrow their document set), the only honest/interpretable v1.

Pipeline (one (entity, query, source) cell — CalPERS / Q2 / D2):
  document  -> [llm_transform] scored_evidence  -> [aggregate] cell_score
                                   |                      |
                                   +---- eval vs ground_truth/gt_* ----+

IMPORTANT HONESTY:
  - We score against a benchmark RECONSTRUCTED from the query definition, NOT
    InfluenceMap's Science-Based Benchmarks DB (which we don't have). Any score
    gap is partly benchmark drift, not pure methodology error.
  - The documents are collective investor statements CalPERS signed; we score
    CalPERS's stance AS A SIGNATORY, which is how InfluenceMap attributed them.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from app.runtime import llm  # reuse the runtime's claude -p backend

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
GT = HERE / "eval" / "ground_truth"
RUN = HERE / "runs" / "level2_calpers_q2_d2"

ENTITY = {"company_id": "C:california_public_employees_retirement_system_calpers",
          "name": "California Public Employees Retirement System (CalPERS)"}
QUERY = {"query_id": "Q2",
         "name": "Climate Science Stance: Does the organization support a science-based response to the climate crisis?"}
SOURCE = {"source_id": "D2", "name": "Corporate Media"}

# Benchmark RECONSTRUCTED from the query definition + the public -2..+2 scale.
# NOT InfluenceMap's benchmark DB. Labeled as such in outputs.
SCALE = """Score the entity's position on the -2..+2 LobbyMap scale:
  +2 strongly SUPPORTS a science-based, Paris/IPCC-aligned response to climate change
  +1 weakly supports
   0 mixed / neutral / no clear position
  -1 weakly opposes
  -2 strongly opposes / actively undermines the science-based response
The score reflects alignment with the science-based-response pole, NOT a normative
judgment about whether that pole is correct."""

PROMPT = """You are reproducing InfluenceMap's LobbyMap evidence scoring.

ENTITY: {entity} — is ONE SIGNATORY of the document below. By signing, the entity
endorses the document's positions; score the entity's stance as a signatory.

QUERY ({query_id}): {query}

{scale}

DOCUMENT (title: {title}, {year}):
\"\"\"
{doc_text}
\"\"\"

Decide whether, by signing this document, {entity} takes a stance on the query.
If yes, extract the single most representative VERBATIM passage and score it.
Reply with ONLY this JSON, no prose:
{{"takes_stance": true/false, "quote": "<verbatim passage or empty>", "score": <int -2..2 or null>, "confidence": <0..1>, "rationale": "<one sentence>"}}"""

DOC_CHARS = 18000


def run():
    docs = [json.loads(l) for l in (DATA / "documents.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    RUN.mkdir(parents=True, exist_ok=True)
    print(f"backend: {llm.resolve_backend()}  |  {len(docs)} documents\n")

    scored = []
    for d in docs:
        prompt = PROMPT.format(entity=ENTITY["name"], query_id=QUERY["query_id"],
                               query=QUERY["name"], scale=SCALE, title=d["title"],
                               year=d["published_date"], doc_text=d["raw_text"][:DOC_CHARS])
        try:
            r = llm.call_llm_real(prompt, model="haiku")
        except Exception as e:
            r = {"_error": str(e)}
        if not isinstance(r, dict) or r.get("_error"):
            print(f"  {d['doc_id']}: LLM error: {r}"); continue
        if not r.get("takes_stance"):
            print(f"  {d['doc_id']}: no stance"); continue
        row = {
            "evidence_id": f"{ENTITY['company_id']}::{QUERY['query_id']}::{SOURCE['source_id']}::{d['doc_id']}",
            "doc_id": d["doc_id"],
            "company_id": ENTITY["company_id"], "influencer_id": None,
            "query_id": QUERY["query_id"], "source_id": SOURCE["source_id"],
            "benchmark_id": "RECONSTRUCTED:Q2_from_query_definition",
            "quote": (r.get("quote") or "")[:600],
            "score": r.get("score"),
            "confidence": r.get("confidence"),
            "evidence_date": d["published_date"],
            "_rationale": r.get("rationale"),
        }
        scored.append(row)
        print(f"  {d['doc_id']}: score={row['score']} conf={row['confidence']}  \"{row['quote'][:60]}...\"")

    # aggregate -> cell_score (mean of scored evidence, rounded; status=scored)
    vals = [e["score"] for e in scored if isinstance(e["score"], (int, float))]
    if vals:
        mean = sum(vals) / len(vals)
        cell = {"company_id": ENTITY["company_id"], "influencer_id": None,
                "query_id": QUERY["query_id"], "source_id": SOURCE["source_id"],
                "status": "scored", "score": round(mean), "cell_score_mean": round(mean, 3),
                "evidence_ids": [e["evidence_id"] for e in scored]}
    else:
        cell = {"company_id": ENTITY["company_id"], "influencer_id": None,
                "query_id": QUERY["query_id"], "source_id": SOURCE["source_id"],
                "status": "NS", "score": None, "cell_score_mean": None, "evidence_ids": []}

    (RUN / "our_scored_evidence.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in scored) + "\n", encoding="utf-8")
    (RUN / "our_cell_score.json").write_text(json.dumps(cell, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── EVAL vs ground truth ──
    gt_cell = json.loads((GT / "gt_cell_score.jsonl").read_text(encoding="utf-8").splitlines()[0])
    gt_ev = [json.loads(l) for l in (GT / "gt_scored_evidence.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    gt_ev_vals = [e["score"] for e in gt_ev if isinstance(e["score"], (int, float))]

    report = {
        "cell": "CalPERS / Q2 (Climate Science Stance) / D2 (Corporate Media)",
        "benchmark": "RECONSTRUCTED from query definition (NOT InfluenceMap's benchmark DB)",
        "ours": {"n_evidence": len(scored), "evidence_scores": [e["score"] for e in scored],
                 "cell_score": cell["score"], "cell_mean": cell["cell_score_mean"]},
        "ground_truth": {"n_evidence": len(gt_ev), "evidence_scores": gt_ev_vals,
                         "cell_score": gt_cell["score"]},
        "agreement": {
            "cell_score_match": cell["score"] == gt_cell["score"],
            "cell_abs_error": (abs(cell["score"] - gt_cell["score"]) if cell["score"] is not None else None),
            "n_evidence_ours_vs_theirs": [len(scored), len(gt_ev)],
        },
    }
    (RUN / "eval_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 64)
    print("EVAL — CalPERS / Q2 / D2")
    print(f"  ours:  {len(scored)} evidence, scores {report['ours']['evidence_scores']}, "
          f"cell = {cell['score']} (mean {cell['cell_score_mean']})")
    print(f"  theirs:{len(gt_ev)} evidence, scores {gt_ev_vals}, cell = {gt_cell['score']}")
    print(f"  CELL MATCH: {report['agreement']['cell_score_match']}  "
          f"(abs err {report['agreement']['cell_abs_error']})")
    print(f"  (benchmark reconstructed, not InfluenceMap's)")
    print("=" * 64)
    print(f"\nartifacts -> {RUN}")


if __name__ == "__main__":
    run()
