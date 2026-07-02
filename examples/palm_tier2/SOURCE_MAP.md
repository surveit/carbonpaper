# Palm-mill source map — where the rich data actually is

From 4 deep-research subagents (one per under-collected mill). The headline: the
pipeline's `locate` was finding the RSPO **public announcement** (capacity only);
the real data is in the **full RSPO audit series** (initial cert + ASA-1..4 +
recertification "Public Summary Reports") and the **CDM Project Design Documents**
for biogas mills. Each of those carries 20–30 sourced fields.

These extracted values double as the EXTRACT eval ground-truth (the fixture set
the compiler left as TODO).

## Document hierarchy (thin → rich)
1. RSPO **public announcement** PDF — capacity, audit dates, cert body. (~1 field; what we had.)
2. RSPO **ASA / recert Public Summary Report** PDF — *the prize*: PalmGHG GHG table
   (kg CO2e/t CPO + POME methane-capture split %), OER/KER, capacity, cert no.+dates,
   planted/peat area, CPO/PK/FFB production, supply base. 150–400 pages.
3. **CDM PDD** (cdm.unfccc.int) for biogas mills — POME m³, COD, biogas MW, methane
   factors, FFB, 10-yr tCO2e schedule. Note: registered ≠ operated (check CER issuance).
4. Press/registries for PROPER + corporate context (weaker, secondary).

## Host hierarchy + access (what worked)
- `rspo.org/wp-content/uploads/*.pdf` — mirrors of many ASA/recert reports; reachable.
- Cert-body sites — **bsigroup.com** (Buatan I ASA-4, direct), Control Union, Sucofindo/SICS, SGS, Mutuagung.
- **cdm.unfccc.int** — PDDs + registry pages, direct.
- Operator sites (musimmas.com) — **504 on HTML pages, but the static `/wp-content/uploads/**.pdf` assets serve via a rendering fetcher**; curl 504s. Retry-on-504 works; the 504 is load/path-dependent, not a block.
- ALL RSPO/CDM PDFs: **download bytes + parse locally (pypdf)** — inline fetch-tool PDF parsing fails on their compressed streams. (Our pipeline already does this.)

## Per-mill: best doc + key recovered fields (all sourced, high-confidence unless noted)

### SUNGAI LILIN — PT Hindoli / Cargill (PO1000000058)
- Best: SICS/Sucofindo **ASA-3** full report (rspo.org mirror) `Cargill_Hindoli_Report_Scheme_Smallholder_Sungai_Lilin_Mill_and_Tanjung_Dalam_Mill_ASA_3_Final.pdf`
- GHG **947.86 kg CO2e/t CPO** (FY15-16) · POME **19-pond IPAL + land application, NO methane capture** · cert **RSPO 00013** (Sucofindo, 25 Feb 2014) · CB lineage BSi→Sucofindo→Control Union · capacity 100 (2023 announcement) vs **120** (ASA-3) vs 160 (combined-cert) — DISCREPANCY, do not merge · no CDM project found.

### BATANG KULIM — PT Musim Mas (PO1000000054)  [was 0 fields]
- Best: **2023 Recertification Public Summary** (169pp) `musimmas.com/wp-content/uploads/2024/08/2023-RSPO-Audit-Report-PT.-Musim-Mas-Batang-Kulim-POM.pdf` (rendering-fetch; curl 504s) + CDM PDD #6889.
- cert **CU-RSPO-819846** (06 Jan 2024→05 Jan 2029) · capacity **60 MT/hr** · OER **22.47%** / KER **6.17%** · PalmGHG **3.95 tCO2e/t CPO** · POME **100% anaerobic → 54% methane→electricity, 46% flaring** · peat **6,293 ha planted on peat** · CDM biogas **48,845 tCO2e/yr**, 3 engines.

### AGROWIRATAMA — PT Agro Wiratama / Musim Mas (PO1000000092)
- Best: **CDM PDD #6872** (cdm.unfccc.int) + RSPO recert stakeholder notice (rspo.org mirror).
- biogas **2×1,063 kW** · POME 0.623 m³/t FFB · FFB 202,707 t/yr · ER **37,565 tCO2e/yr DESIGNED — zero CERs issued (registered, not operated)** · capacity **45 MT/hr** · CPO 40,044 / PK 10,972 MT.
- Full RSPO report: behind musimmas.com 504 AND not on rspo.org/Wayback → genuinely unavailable this pass.
- Caught a NAME COLLISION: a different "PT Agrowiratama" in W. Kalimantan — do not merge.

### BUATAN I — PT Inti Indosawit Subur / Asian Agri-RGE (PO1000000021)  [was ~2 fields]
- Best: **BSI ASA-4** report (387pp, 2025) `bsigroup.com/siteassets/pdf/en/about-us/rspo/rspo-public-summary-pt-inti-indosawit-subur-buatan-I-pom-asa2-4-approved.pdf`
- cert **RSPO 638918** (24 Aug 2021→23 Aug 2026) · capacity **60 MT/hr** · OER **18.43%** / KER **4.81%** · GHG **40 kg CO2e/t CPO** · POME **97% methane→electricity, 3% flaring; biogas plant since 2015 (~2MW Kubota AnMBR)** · **0 ha on peat** · CPO 37,446 t/yr.

## Implications for the pipeline (where the 90% was being left)
- **locate**: target the full audit SERIES (ASA/recert Public Summary) on rspo.org +
  cert-body sites + the CDM registry — not the announcement. URL patterns are
  discoverable (`site:rspo.org/wp-content/uploads "{operator}" "{mill}" filetype:pdf`;
  `bsigroup.com/siteassets/pdf/en/about-us/rspo/...{mill}...approved.pdf`).
- **fetch**: rendering-fetch + **retry-on-504** + Wayback fallback; go to PDF assets, skip HTML.
- **pdf_to_text**: raise/chunk the 60K-char cap — these reports are 150–400 pages and
  the GHG appendix is at the END.
- **extract**: widen the field set — PalmGHG GHG + POME methane-split %, OER/KER, cert
  no.+dates, areas incl. peat, production, supply base, biogas MW.
- **adjudicate**: the judgment these surfaced is exactly right — capacity discrepancies
  (flag, don't merge), CDM designed-vs-operated, name collisions, press-vs-primary.
