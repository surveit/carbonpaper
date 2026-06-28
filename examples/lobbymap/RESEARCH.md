# LobbyMap (InfluenceMap) — methodology & data-availability research

Research pass before implementing the methodology as a DAG artifact. Goal: mirror
InfluenceMap's *actual* LobbyMap scoring closely enough to build an eval dataset
from their published results and test our pipeline against it.

**Epistemic status of this note:** compiled 2026-06-27 from InfluenceMap's public
pages (cited inline). Observations are marked vs. inferences. The aggregation
algorithm is explicitly *proprietary* and is the main thing we cannot fully
reproduce — see §4.

## Sources consulted (provenance)

- 🟣 https://lobbymap.org/Methodology-Portal — portal/index (JS-rendered tables not visible to static fetch)
- 🟣 https://lobbymap.org/multipage/FAQ-f9a8629330cd161eba0d9ed43ba17965-785703 — scoring mechanics, "96 scoring cells", weighting prose
- 🟣 https://lobbymap.org/company/OMV-9575d9222925fe611b993356d67f507c — a full company scorecard that **renders server-side** (this is the key data-access finding)
- 🟣 https://lobbymap.org/LobbyMapScores — the master scores table (JS-rendered; needs a browser to extract)
- 🟣 https://influencemap.org/briefing/Global-Leaders-in-Climate-Policy-Engagement-2024-29339 — leaders briefing
- Company slugs found: Chevron `…f4b47c4ea77f…`, ExxonMobil `/company/Exxon-Mobil`, OMV `…9575d92…`

## 1. The scoring structure (observation)

LobbyMap scores each entity on a **matrix of (policy query × data source) cells**.

- **Queries** — policy areas. From the OMV scorecard, the query rows include:
  Communication of Climate Science; Alignment with IPCC on Climate Action;
  Supporting the Need for Regulations; Support of UN Climate Process;
  Transparency on Legislation; Carbon Tax; Emissions Trading; Energy & Resource
  Efficiency; Renewable Energy; Energy Transition & Zero Carbon Technologies;
  GHG Emission Regulation; Disclosure on Relationships; Land Use.
  (~13 visible for OMV; the FAQ says the full grid is **96 scoring cells**, so the
  canonical taxonomy is larger — *to confirm against a company with fuller
  coverage or the methodology doc.* Evidence items are referenced as `Q<n>-D<n>`,
  e.g. `Q10-D4`, implying ~16 queries × ~7 data sources.)

- **Data sources** (the columns, ~7 for OMV): Main Website, Corporate Media,
  CDP Responses, Government/Direct Consultation, Media Reports, CEO Messaging,
  Financial Disclosures.

- **Cell score**: each (query × data source) intersection scored on a **5-point
  scale −2..+2**, where +2 = full support for Paris/IPCC-aligned policy, −2 =
  active opposition, 0 = mixed/neutral. Cells can also be **NS** (not scored) or
  **NA** (not applicable).

## 2. Evidence items (observation)

Each scored cell is backed by **individual, dated, hyperlinked evidence items**,
each with: data-source type, date, score (−2..+2), and a descriptive excerpt
linking to the original document. Examples from OMV:

- `Q1-D6` CEO Messaging, **+1**: Paris/EU-target commitment in a May 2025 Industry Association Update.
- `Q10-D4` Government Consultation, **−1**: opposed CCS storage mandates in April 2025 EU comments.
- `Q9-D3` CDP Responses, **−1**: limited renewable support relative to fossil continuation.

InfluenceMap assesses **~30,000 evidence items/year** across ~1,000 companies +
~330 industry associations. **This is the layer that gives us a clean
evidence-level eval: their evidence text is our input, their −2..+2 is the label.**

## 3. The four output scores (observation)

| Score | Range | Meaning |
|---|---|---|
| **Organization Score** | 0–100% | How supportive/obstructive the company's *direct* engagement is. <50 = misaligned, <25 = material opposition. (Rescale of the −2..+2 cell grid, weighted.) |
| **Relationship Score** | 0–100% | Engagement via industry associations. Each association carries a "strength of relationship" % (e.g. OMV–Hydrogen Europe 63%) and its own score. |
| **Performance Band** | A+ … F | Org + Relationship combined → overall. **20 bands**: A+ = 95–100%, down to E− = 25–30%, **F = <25%**. (OMV: Org 52%, Rel 35% → band **D+**, "50%".) |
| **Engagement Intensity** | 0–100 | How *much* the company engages (regardless of direction). EI>35 = relatively heavy engagement. Recency-weighted (last 3 yrs emphasized). |

## 4. Aggregation = proprietary (the honest constraint)

The FAQ states the Organization Score is computed "by our proprietary algorithm
that accounts for weightings and irrelevant data sources/queries." Known factors:

- **Recency weighting**: evidence >3 years old "does not significantly contribute."
- **NA/NS redistribution**: missing cells' weight redistributed to present cells.
- **Per-source / per-query relevance weights**: undisclosed.

**Implication for the eval (inference):** we can reproduce the *evidence-tier*
cleanly (our −2..+2 vs theirs on the same evidence), and *approximate* the query-
and company-tiers. We **cannot** exactly reproduce the 0–100 Organization Score
because the weights are undisclosed — so the company-tier metric must be framed as
"how close does our reconstructed aggregation land," not "exact match," and the gap
is itself a reportable finding. We will **not** fabricate their weights.

## 5. Data access (observation) — the enabling finding

Per-company scorecard pages **render server-side** and expose the entire tiered
structure (band, org/rel scores, per-cell −2..+2, association relationships, and
the underlying evidence items with source links). So ground truth is **scrapeable
per company without any API or paid export** — subject to a robots.txt/ToS check
before we build the connector. The master `LobbyMapScores` table and some matrix
interactions are JS-rendered and will need a headless browser to enumerate the
company universe.

## 6. Proposed v1 sector slice

**Oil & gas majors** (~10–20 cos): ExxonMobil, Chevron, Shell, BP, TotalEnergies,
ConocoPhillips, OMV, Eni, Equinor, Repsol, Marathon, Phillips 66, Woodside, etc.
Rich evidence per company, heavy engagement (high EI), wide score spread — ideal
for an agreement-rate eval, and InfluenceMap publishes a dedicated "supermajors"
comparison we can sanity-check against.

## Open items to confirm

- [ ] robots.txt / ToS for scraping lobbymap.org company pages.
- [ ] Full canonical query taxonomy (the 96-cell grid: exact query list × data-source list).
- [ ] Exact Performance Band thresholds for all 20 bands (have endpoints: A+ 95–100, E− 25–30, F <25).
- [ ] Whether evidence-item original-source links are stable enough to cite.
