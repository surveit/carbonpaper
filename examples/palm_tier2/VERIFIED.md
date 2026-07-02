# Verified run — 2026-06-29 (run 20260629T160736)

The hardened pipeline's first complete end-to-end run (agent_sdk backend, 3 mills
chosen to span the failure modes). Status `ok`, all 10 stages green, ~9.5 min
total (locate 156s, extract 226s, adjudicate 86s). Fetch 29/31 (2 misses were
404s on speculative rspo.org URL patterns; other docs covered those mills).

## Per-mill result (before = pre-hardening run 20260619T153955)

| mill | group / failure mode | before | after | truth overlap |
|---|---|---|---|---|
| BATANG KULIM | Musim Mas — 504-blocked, was empty | 0 | **15 fields** | 9/10 |
| SUNGAI LILIN | Cargill — UA-blocked, was thin | 1 | **10 fields** | 4/6 |
| BUKIT MARADJA | SIPEF — was best-case | 6 | **15 fields** | (no truth rows) |

All 40 fields graded primary, nearly all high-confidence: capacity, CPO/FFB
production, OER/KER, PalmGHG tCO2e/tCPO, POME treatment + methane split,
certificate number/dates/body, planted + peat area, supply base + chain model.
Spot-checks against SOURCE_MAP.md ground truth: Batang Kulim cert CU-RSPO-819846 ✓,
OER 22.47 ✓, PalmGHG 3.95 ✓; Sungai Lilin PalmGHG 947.86 kg CO2e/t CPO ✓;
Bukit Maradja cert RSPO 632266 ✓, 30 t/hr ✓ — and it recovered the 2020→2025
recert dates the earlier manual research couldn't confirm.

Full reconciled output: `eval/verified_run_20260629T160736.json`.
The run directory itself is gitignored; re-run with
`CW_LLM_BACKEND=agent_sdk python -m app.runtime.runner examples/palm_tier2`.

Notable: locate averaged ~52s/mill here — much faster than the ~3-4 min/mill
seen earlier; the "locate is too slow" concern is smaller than feared.
