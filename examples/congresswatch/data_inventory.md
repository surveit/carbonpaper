# CongressWatch — Data Inventory

Source: `~/Downloads/data/data/{congress_press, house, senate}/`. Survey performed 2026-05-21 by parallel exploration agents.

## congress_press/  (JSONL press releases)

- **137,758 records** across 51 JSONL files (~490 MB)
- Years 2022–2025 complete (~20–48k/yr); 2026 partial (Jan–Mar, ~7.9k records)
- Schema (top-level): `url, title, text, date, date_source, source, domain, scraper, member, collected_at, updated_at`
- `member` object (99.9% populated): `bioguide_id, name, state, party, chamber`
- **`member.bioguide_id` is the entity-key gold standard** — joins cleanly to Congress.gov and most other downstream datasets
- Text: median 2,117 chars, max 36,877. 78 records (0.06%) have empty text (JS-rendered/paywalled).
- Encoding: occasional `â€™` artifacts (~17 instances, mostly 2022–2023 titles).
- Dates: clean YYYY-MM-DD throughout.
- 0 duplicate URLs (already deduplicated).

## house/  (XML quarterly disclosures, Lobbying Disclosure Act)

- **409,650 XML files**, ~4.8 GB. 22 year-quarter folders + 5 annual `Registrations` folders.
- Root element: `<LOBBYINGDISCLOSURE2>`. Stable schema across all years.
- Key fields: `organizationName` (registrant), `clientName`, `senateID`, `houseID`, `reportYear`, `reportType` (Q1–Q4, RR, 1T, 1A), `income`, `expenses`
- Activity sub-element: `<alis>/<ali_info>` with `<issueAreaCode>`, `<specific_issues><description>` (free text), `<federal_agencies>`, repeated `<lobbyist>` elements.
- **68 unique issue codes** seen in 500-file sample. Top: BUD (105), TAX (69), HCR (63), DEF (48), TRA (45), TRD, ENG, MMM, AGR, EDU.
- **Big caveat:** filings do NOT name specific members of Congress. `<federal_agencies>` lists "U.S. SENATE" / "U.S. HOUSE OF REPRESENTATIVES" as monoliths. Bill numbers in `<description>` are the only cross-link to specific congressional activity, and they are non-normalized (H.R., HR, H., S. formats).
- ~50% of filings have `noLobbying=Y` (termination or no-activity quarters); ~30% lack income/expenses.

## senate/  (JSON annual disclosures, LDA + contributions)

- Per year: `filings_<year>.json` (400–470 MB), `contributions_<year>.json` (100 MB).
- **JSON format, flatter than House XML.** Single big array per file from `lda.senate.gov` API.
- Filings schema: `filing_uuid, filing_type, filing_year, filing_period, dt_posted, income, expenses, registrant {…}, client {…}, lobbying_activities [ {general_issue_code, description, lobbyists, government_entities, …} ]`.
- **Contributions** (House lacks this): individual lobbyist's PAC contributions, schema `{filing_uuid, registrant, lobbyist, pacs, contribution_items}`. Most records have `no_contributions=true`; meaningful contribution detail is sparse.
- `constants/lobbying_activity_issues.json` (79 codes), `government_entities.json` (~300 agencies), `filing_types.json` etc. — useful lookup tables. **House and Senate issue codes are aligned.**

## Cross-link strategy

The hard truth: **lobbying disclosures do not directly name members of Congress**. Three viable connections:

1. **Issue code + time window** (loose, easy): "Lobbyist X filed on HCR in Q4 2025; Member Y issued press releases on HCR in Jan 2026." Statistical pattern, not causal.
2. **Bill number normalization** (medium effort): parse bill numbers out of `<description>` → join to Congress.gov sponsor/cosponsor data → know which members touched the bill.
3. **Lobbyist→government work history**: `coveredPosition` field names prior gov roles. Could surface "lobbyist was former Chief of Staff to Member X" — strongest narrative link, but requires a roster of staff↔member relationships we don't have.

For the prototype slice we use (1).
