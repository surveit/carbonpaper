# Methodology: Entity-focused sustainability research via RSPO and certification data

## Stage 1: Seed entity (input_data)
We begin with a known facility identifier: PT Hindoli Sungai Lilin (UML RSPO_PO1000000058), located in Musi Banyuasin, South Sumatra, Indonesia. This is a single seed row—just enough to bootstrap the search.

## Stage 2: Build search queries (python_transform)
From the facility name and region, generate deterministic search query templates:
- RSPO + mill name + CARGILL (identity confirmation)
- Mill name + RSPO public summary report (certification document)
- RSPO certificate + PalmTrace ID (direct lookup)
- PROPER rating + region (Indonesian environmental rating)
- Certification body + mill name (authority ranking)
- Biomass/methane queries (energy systems disambiguation)
- USAID/Winrock queries (historical projects)
- News and regulatory sources (secondary confirmation)

Output: ~13 search queries, each tagged with intent.

## Stage 3: Locate documents (llm_transform)
The LLM uses WebSearch to find documents matching each query. For each hit, the LLM records: URL, title, doc type (RSPO announcement, RSPO P&C report, permit, news, etc.), certification body, audit year, and confidence.

The LLM applies judgment: RSPO-official > certification body reports > news/PR > unverified. Recent (2020+) > older.

Output: ~30-50 candidate URLs ranked by authority and recency.

## Stage 4: Fetch documents (python_transform)
For each unique URL:
1. Fetch via WebFetch (handles redirects, retries on 403/timeout)
2. If PDF: extract text using pypdf or pdfplumber
3. If HTML: extract visible text
4. Record extraction success and any errors

This is deterministic Python; no judgment involved. Output raw document text.

## Stage 5: Extract structured fields (llm_transform)
The LLM reads each document's raw text and extracts structured fields: certificate number, body, audit type, validity dates, FFB capacity (MT/hour or MT/year), annual CPO and PK production, GHG figure (kg CO2e per tonne CPO), POME treatment system (ponds, biogas, land application, etc.), methane capture capacity (MW), biomass boiler capacity (MW), and other contextual data.

For each field, the LLM returns: value (or null), extraction confidence (high/medium/low), and notes on ambiguity or contradictions within the document.

Why LLM? Documents are semi-structured: mix of prose, tables, boilerplate, and technical sections. An LLM can navigate heterogeneous text; keyword-grep alone would miss context.

Output: One row per source URL, with all extracted fields + confidence levels.

## Stage 6: Consolidate per facility (python_transform)
Group extracted data by facility. For each field (capacity, GHG, POME, etc.), collect all non-null values from all documents, tagged with source URL, document type, year, and extraction confidence.

Create a single row per facility with: current value (from most-recent cert) and historical values (JSON list of {value, source, year, confidence}). Flag conflicts (e.g., capacity 100 MT/hr vs. 120 MT/hr).

This is deterministic aggregation and grouping.

Output: One row per facility, with all fields and conflict flags.

## Stage 7: Adjudicate conflicts (llm_transform)
Where the same field has multiple differing values, the LLM applies domain logic to pick the best one:
- Recency: More recent is more likely current (2023 > 2015)
- Authority: RSPO-official audit > PR > news
- Document type: P&C report > announcement > regulatory > news
- Certification status: Certified figures > estimated

For conflicts in this run:
- **Capacity**: 100 MT/hr (2023, mill-specific) > 120 MT/hr (2015-16, permit ceiling)
- **GHG**: 947.86 kg CO2e/t CPO (2015-16, measured) is best available; no more-recent mill-level figure found
- **Methane vs. biomass**: Two separate systems—0.6 MW methane capture (Winrock pilot, ~2013) + main biomass boilers (ongoing)

Output: One row per facility, with final values, sources, years, confidence levels, and rationales.

## Stage 8: Enrich context (python_transform)
Add canonical identity data from high-confidence sources: facility name, parent company (Cargill), country, region, coordinates, RSPO membership number, PalmTrace ID, supply chain model.

Output: Enriched facility row, ready for publication.

## Stage 9: Render dossier (publish)
Format all adjudicated data + sources as an HTML dossier with: Identity & corporate section (name, location, parent, RSPO ID); Capacity & production (FFB rate, annual CPO/PK); GHG & climate (per-tonne figure, year, source); POME treatment (system type, pond count, monitoring); Renewable energy (biomass boilers, methane capture, homes powered); Certification (cert number, body, validity, audit dates); Data sources & confidence (table of all URLs, doc types, extraction confidence, notes); Research narrative (explanation of methodology, conflicts resolved, dead ends encountered).

Every claim is cited with a footnote link to the source document.

## Key methodological decisions

1. **Identity-first search** (Stages 2-3): Before chasing data, confirm the facility's identity. The UML ID (RSPO_PO1000000058) and coordinates (104.127°E, −2.612°S) were the lock.

2. **Authority ranking** (Stage 3): RSPO-official documents (announcements, P&C reports, certified certs) are more reliable than press releases or news. Recency breaks ties—2023 > 2015.

3. **PDF extraction as deterministic Python** (Stage 4): WebFetch can't parse image-based PDFs, so we extract text locally. This is mechanical, not judgment.

4. **LLM extraction of semi-structured text** (Stage 5): Certification PDFs mix tables, prose, and boilerplate. An LLM can parse these better than keyword-grep. We keep extraction confidence per field, allowing downstream stages to weight uncertain extractions less.

5. **Conflict resolution via domain logic** (Stage 7): When capacity is reported as both 100 and 120 MT/hr, the LLM picks the more recent (2023) as current, noting the older figure likely reflects a permit ceiling. Similarly, methane capture and biomass are separate systems, not alternatives.

6. **Transparent reporting** (Stage 9): The final dossier includes the research narrative, dead ends, and confidence levels. Readers can see why we trust certain figures and where uncertainty remains.
