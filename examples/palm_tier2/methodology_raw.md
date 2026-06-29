# Palm-oil mill OSINT — methodology

*Backed out from the `palm_tier2` DAG: this is the prose spec that, fed to the compiler,
would produce the 10 compiled stages. Normally the input; here reconstructed from the
output (a "decompile"). It doubles as a publishable methodology statement — review the
**method** here, the implementation in `compiled/`.*

## Purpose & scope
For a given set of Indonesian palm-oil mills, reconstruct an **auditable record of
on-site features and emissions** from public **primary** sources — not by trusting any
operator's own summary, and never by estimating a value that isn't documented.

## Input
A seed list of mills, each with: mill name, operator (PT), parent group, province,
**Universal Mill List / PalmTrace id** (the join key across all sources), and coordinates.

## Method

The pipeline is deterministic plumbing with **three points of genuine judgment** (marked
⚖); everything else is mechanical and reproducible.

1. **Targeted source discovery.** From each mill's identity, construct targeted queries
   — keyed on the UML id — against the *known authoritative* hosts: RSPO (`rspo.org`),
   the certification bodies (Control Union, BSI, Sucofindo, Mutuagung…), the UNFCCC CDM
   registry, and — secondarily — PROPER and trade press. (We do not open-endedly "search
   the web"; the source hierarchy is known in advance.)

2. ⚖ **Locate the authoritative, most-recent document.** Among the results, select the
   *full RSPO audit-series Public Summary Report* (Annual Surveillance / recertification)
   and any CDM Project Design Document — **not** the thin RSPO public *announcement*,
   which carries almost no operational data. Prefer the newest by audit type and date.
   This is judgment: documents move, recur per audit year, and the right one is the one
   that actually carries the operational appendix.

3. **Retrieve & render.** Download each document **presenting as a normal browser** (a
   bare bot identity is refused by corporate hosts even for public files), with retry on
   transient failures and an archival (Wayback) fallback. **Cache the raw bytes** and
   extract text locally — the canonical "keep the raw alongside the cooked." A blocked or
   missing document is recorded as such, not silently dropped.

4. **Locate the relevant passages.** Within the rendered text, find the defined field
   anchors — nameplate capacity, the **PalmGHG / "Summary of Net GHG" appendix**, the
   **POME methane-capture split** (electricity vs flaring), OER/KER, certificate number +
   validity dates, planted & peat area, CPO/PK/FFB production, and biogas capacity. (The
   GHG appendix sits at the *end* of a 150–400-page report, so the whole document must be
   in scope, not the first pages.)

5. ⚖ **Extract the defined field set.** From the located passages **only**, extract each
   field as a value + unit + **the source it came from** + a **provenance grade**. Assert
   nothing that is not present in the cited passage; where the source is silent, the value
   is `unknown`. Press sources are graded weaker than certification/registry sources.

6. ⚖ **Reconcile across a mill's documents.** Where sources disagree (e.g. capacity
   differs across audit years), prefer the **most-recent primary source** and record *why*
   — never silently average. Distinguish **designed vs operated**: a *registered* CDM
   biogas project is not evidence it was built or that credits were issued. Reject
   **same-name, different-entity** collisions (two unrelated "PT X").

7. **Publish.** A per-mill dossier of the reconciled fields, each shown with its value,
   confidence, provenance grade, and a link to the exact cited source.

## Sourcing discipline (applies to every step)
- **Primary over secondary.** Certification/registry documents outrank press and operator PR.
- **Every value carries its source** — URL + retrieval + grade — or it is `unknown`.
- **Never fabricate.** A value the model asserts without a locatable source is *flagged,
  not published*; absence of evidence is reported as absence, not filled with an estimate.
- **Reproducible.** The raw bytes behind every value are cached and hashed, so any figure
  can be re-checked against the exact document it came from.

## Verification
A held set of **sourced ground-truth values** (per mill, per field — see `eval/`) measures
extraction agreement, so the method's accuracy is *measured*, not asserted.

## Map to the implementation (for the round-trip check)
| § | stage(s) in `compiled/` | kind |
|---|---|---|
| 1 | `build_queries` | deterministic |
| 2 ⚖ | `locate` | LLM + web |
| 3 | `fetch_docs` → `pdf_to_text` | deterministic |
| 4 | `grep_fields` | deterministic |
| 5 ⚖ | `extract` | LLM |
| 6 ⚖ | `collate` → `adjudicate` | deterministic → LLM |
| 7 | `publish` | deterministic |

`compile(this document)` should reproduce those stages; a diff that *isn't* explained by a
deliberate refinement is a drift between the stated method and what the DAG actually does.

**Round-trip verified** (local compile of this doc, haiku): reproduced **all 10 stages
with the identical node-type sequence** — the 3 LLM judgment points
(`locate` / `extract` / `adjudicate`) and the 5 deterministic transforms — and the same
type histogram `{input_data:1, python_transform:5, llm_transform:3, publish:1}`. The only
deltas were two cosmetic ids (`seed_mills`↔`seeds`, `publish`↔`publish_dossiers`). So this
prose and the DAG agree.
