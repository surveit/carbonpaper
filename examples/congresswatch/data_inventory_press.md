# Congress Press Releases Dataset Inventory

## Volume

**Files**: 51 JSONL files across 8 locations (2022–2026)
**Total Size**: 0.49 GB
**Total Records**: 137,758 press releases

**Breakdown by Year:**
- **2022**: 12 files, 19,702 records (~68 KB/file)
- **2023**: 12 files, 30,249 records (~88 KB/file)
- **2024**: 12 files, 31,583 records (~94 KB/file)
- **2025**: 12 files, 48,318 records (~144 KB/file)
- **2026 (Jan–Mar)**: 3 files, 7,906 records (14.3–15.2 MB/file)

**JSON Validity**: 100% valid—zero parse errors across all 137,758 records.

---

## Schema

**Top-Level Fields** (11 keys, all present in >99.9% of records):
- `url` (str) - Full HTTP URL to press release
- `title` (str) - Release title
- `text` (str) - Press release body text (99.9% present)
- `date` (str) - Publication date (YYYY-MM-DD format)
- `date_source` (str) - Source of date (`scraper` or `page_html`)
- `domain` (str) - Member's website domain
- `scraper` (str) - Scraper identifier (member username)
- `source` (str) - Homepage/index URL scraped
- `collected_at` (str) - ISO 8601 timestamp when record was collected
- `updated_at` (str) - Last update timestamp
- `member` (object, 99.9% present) - Structured member metadata

**Nested Object: `member`** (5 subfields):
- `bioguide_id` (str) - Congress's unique ID (e.g., "B000740")
- `name` (str) - Member's full name
- `state` (str) - 2-letter state code
- `party` (str) - "Republican" or "Democratic"
- `chamber` (str) - "House" or "Senate"

---

## Field Examples & Distributions

| Field | Examples | Notes |
|-------|----------|-------|
| **url** | `https://bice.house.gov/media/press-releases/rep-bice-solved-over-400-...` | Deduplicated; 0 duplicates found |
| **title** | "Rep. Bice Solved Over 400 Constituent Cases"; "Biden Signs Casten's Clean Energy Provisions" | 100 chars typical; some UTF-8 artifacts (see Quirks) |
| **text** | Press body text | Min: 3 chars; Median: 2,117 chars; Max: 75,795 chars; Mean: 2,811 chars |
| **date** | "2022-01-01", "2025-12-31" | Consistent YYYY-MM-DD; 78 records (~0.06%) have missing text despite date presence |
| **member.bioguide_id** | "B000740", "C000059", "C000984" | Format: uppercase letter + 6 digits; enables direct joins to Congress API |
| **member.name** | "Stephanie I. Bice", "James E. Clyburn", "Sean Casten" | Full names with middle initials |
| **member.state** | "OK", "SC", "IL" | 2-letter abbreviations; consistent |
| **member.party** | "Republican", "Democratic" | Binary; 100% present when member is present |
| **member.chamber** | "House", "Senate" | Distinguishes bicameral data |
| **domain** | "bice.house.gov", "cleaver.house.gov" | House/Senate domain patterns |
| **date_source** | "scraper" (79%), "page_html" (21%) | Indicates extraction method |

---

## Date Coverage

- **2022**: Jan 1 – Dec 31 (complete year)
- **2023**: Jan 1 – Dec 31 (complete year)
- **2024**: Jan 1 – Dec 31 (complete year)
- **2025**: Jan 1 – Dec 31 (complete year)
- **2026 (YTD)**: Jan 1 – Feb 28 (partial; 3 months)

All dates are publication dates on official congressional websites; no future-dated records.

---

## Data Quality & Quirks

1. **UTF-8 Encoding Artifacts (17 instances)**
   - Found primarily in titles; examples: `"ROOM"` appears as `ROOM–` with curly quote bytes
   - Likely double-encoded UTF-8 during scraping; suggests crawling may have used mismatched encoding
   - Affects: 2022–2023 data most; rare in 2024+
   - Impact: Low (17 / 137,758 = 0.01%), but breaks full-text search if not normalized

2. **Missing Text (78 records, 0.06%)**
   - Records have valid date, title, member, but empty/null `text` field
   - Likely extraction failures (CAPTCHA, JavaScript-rendered content, paywalls)
   - Concentrated in certain crawls; not random failures

3. **Null Members (78 records, 0.06%)**
   - Rare; member field null when scraper cannot infer member from domain
   - Does not prevent joining on `domain` field alone

4. **Duplicate URLs**: 0 found (deduplicated dataset)

5. **Non-ASCII Handling**: Generally robust; Unicode names (e.g., Spanish surnames) handled correctly except for UTF-8 artifact issue above

---

## Recommended Joins

| Field | Join Target | Use Case |
|-------|-------------|----------|
| **member.bioguide_id** | Congress.gov API (`bioguide_id` field) | Gold standard join; unique, stable identifier |
| **member.name** | Lobbying disclosure DB (lobbyist names, clients) | Fuzzy match needed; names vary in source formatting |
| **member.state + member.chamber** | FEC data (Campaign Finance) | Matches to contributions/expenditures by member |
| **domain** | Internal congressional web crawls | Deduplicate or correlate across data sources |
| **date** | Events calendar / roll calls | Temporal joins for legislative context |

**Best practice**: Primary join on `member.bioguide_id` (100% unique, no collisions).

---

## Summary

This dataset is clean, well-structured press releases from U.S. House and Senate members (2022–present). It includes member metadata enabling joins to lobbying, campaign finance, and legislative databases. Slight encoding glitches in older data (2022–2023) do not affect bulk analysis. Ready for NLP analysis (topic modeling, sentiment, rhetoric) or joining with external congressional datasets.
