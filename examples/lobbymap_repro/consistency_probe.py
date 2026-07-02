"""Quick probe: (a) self-consistency of the scorer, (b) does injecting ALL six
sectoral benchmarks (vs only sector-relevant ones) throw scoring off?

60-row stratified sample x 3 runs: A1, A2 = all-benchmarks (repeat -> consistency);
C = benchmarks filtered by the entity's sector (finance/consumer entities get NONE).
Sector relevance is by ENTITY (not invented per-query mapping).
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from app.runtime import llm

HERE = Path(__file__).resolve().parent
GT = HERE.parent / "lobbymap" / "eval" / "ground_truth"

SECTOR_BM = {  # entity -> relevant benchmark technologies (by what the entity does)
    "C:exxon_mobil": ["Oil", "Fossil Gas", "Coal"],
    "C:total": ["Oil", "Fossil Gas", "Coal"],
    "C:toyota_motor": ["Light-duty Road Transport"],
    "C:iberdrola": ["Renewables", "Fossil Gas", "Coal"],
    "C:unilever": [], "C:california_public_employees_retirement_system_calpers": [],
}

def main() -> None:
    tmpl = None
    import yaml
    stage = yaml.safe_load(open(HERE / "compiled" / "02_scored_evidence.yaml", encoding="utf-8"))
    tmpl = stage["llm"]["prompt_template"]
    bm = [json.loads(l) for l in open(GT / "benchmarks.jsonl", encoding="utf-8")]
    all_ctx = "\n\n".join(f"[{b['technology']}] {(b['benchmark_text'] or '')[:1400]}" for b in bm)
    by_tech = {b["technology"]: b for b in bm}

    df = pd.read_parquet(HERE / "data" / "scoring_targets.parquet")
    ev = df[df["target_id"].str.startswith("EV::")].copy()
    gt = {json.loads(l)["evidence_id"]: json.loads(l)["score"] for l in open(GT / "gt_scored_evidence.jsonl", encoding="utf-8")}
    ev["gt"] = ev["target_id"].str.replace("EV::", "", regex=False).map(gt)
    ev = ev[ev["gt"].notna()]
    sample = pd.concat([g.sample(min(12, len(g)), random_state=7)
                        for _, g in ev.groupby(ev["gt"].astype(int))]).reset_index(drop=True)
    print(f"sample: {len(sample)} rows, gt dist: {sample['gt'].astype(int).value_counts().sort_index().to_dict()}")

    def run(tag: str, ctx_fn) -> list:
        rows = []
        for _, r in sample.iterrows():
            row = dict(r)
            row["benchmark_context"] = ctx_fn(r)
            rows.append(row)
        res = llm.call_llm_batch("probe_" + tag, {"prompt_template": tmpl, "model": "haiku"}, rows, parallel=8)
        out = []
        for r in res:
            s = r.get("score") if isinstance(r, dict) else None
            out.append(int(s) if isinstance(s, (int, float)) and s is not None else None)
        return out

    a1 = run("a1", lambda r: all_ctx)
    a2 = run("a2", lambda r: all_ctx)
    c = run("c", lambda r: "\n\n".join(
        f"[{t}] {(by_tech[t]['benchmark_text'] or '')[:1400]}" for t in SECTOR_BM.get(r["entity_id"], [])) or
        "(no sectoral benchmark applies to this entity; use the scale definitions alone)")

    res = sample[["target_id", "entity_id", "gt"]].copy()
    res["a1"], res["a2"], res["c"] = a1, a2, c
    res["gt"] = res["gt"].astype(int)
    res.to_json(HERE / "consistency_probe_results.jsonl", orient="records", lines=True)  # save BEFORE stats
    both = res.dropna(subset=["a1", "a2"])
    print(f"\nSELF-CONSISTENCY (a1 vs a2): exact {(both["a1"] == both["a2"]).mean():.1%} "
          f"within1 {((both["a1"] - both["a2"]).abs() <= 1).mean():.1%}  (n={len(both)})")
    for tag in ("a1", "a2", "c"):
        m = res.dropna(subset=[tag])
        print(f"vs GT [{tag}]: exact {(m[tag] == m["gt"]).mean():.1%}  within1 {((m[tag]-m["gt"]).abs()<=1).mean():.1%}  "
              f"posbias(theirs<=0,ours>=1) {int(((m["gt"]<=0)&(m[tag]>=1)).sum())}  n={len(m)}")
    print("\nper-entity exact (all-bm a1 vs sector-filtered c):")
    for eid, g in res.groupby("entity_id"):
        g1, gc = g.dropna(subset=["a1"]), g.dropna(subset=["c"])
        print(f"  {eid[:44]:44} a1 {(g1["a1"]==g1["gt"]).mean():.0%} ({len(g1)})  c {(gc["c"]==gc["gt"]).mean():.0%} ({len(gc)})")

if __name__ == "__main__":
    main()
