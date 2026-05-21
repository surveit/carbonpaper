---
stage_number: 2
stage_id: source_ingestion
stage_name: Data-source ingestion
source_doc: examples/lobbymap/methodology_raw.txt
source_lines: [241, 252]
related_sections: ["§3.2", "§4.5 (referenced as 'D1–D8' coding)"]
---

# Stage 2: Data-source ingestion

## Prose excerpt

### Lines 241–252 (§3.2 Defining Data Sources / Table 3)

> InfluenceMap's methodology involves the identification of evidence related to corporate climate policy engagement, which is assessed against government and science-based policy benchmarks. The criteria for selecting data sources are as follows: sources must be publicly available, should ideally be applicable to all entities within our assessed universe, and must offer reliable insights into corporate activities and behaviors.
>
> These criteria are generally applicable and accessible in the public domain for all large companies, with the exception of CDP (formerly known as the Carbon Disclosure Project) and financial disclosures, which do not apply to industry associations.
>
> **Table 3: LobbyMap Data Sources Used to Define Corporate Climate Policy Engagement**
>
> | Data Source | Description |
> |---|---|
> | Organizational Websites | The main organizational web site of the company and its subsidiaries. |
> | Corporate Media | Additional media communications controlled by the organization, including social media channels. |
> | CDP Disclosures | Responses to questions within CDP's system (12.3) related to climate policy engagement. |
> | Direct Consultation with Governments | Comments from the entity submitted through official regulatory and legislative consultation processes or via meetings and other direct engagements with policymakers. This includes evidence obtained by InfluenceMap through Freedom of Information requests. |
> | Reliable Media | Reports of corporate climate policy engagement by well-established media. |
> | Management Messaging | Direct quotes or transcripts of statements by an entity's CEO/Chairman under a variety of circumstances. |
> | Financial Disclosures and Investor Transcripts | Submissions by the company to financial regulators, as well as officially recorded transcripts of company-investor calls. |
> | Lobbying Disclosures (Proposed)* | InfluenceMap is considering an additional data source to track and assess information provided by companies through government lobbying disclosure channels such as the EU Transparency Register. |
> | Advertising (Proposed)* | InfluenceMap is considering an additional data source to track and assess the use of paid-for, targeted climate advertising (e.g., Facebook/Instagram/google adverts, advertorials, etc.) |
>
> *Data sources labeled "Proposed" are under consideration for incorporation into the LobbyMap platform, following consultation and further development
>
> These data sources encompass many, if not most, of the various aspects of corporate climate policy engagement discussed above. For instance, "Direct Consultation with Governments" (D4) includes specific "internal" lobbying tactics and serves as an essential means through which corporations and industry groups convey their positions on policy matters and desired outcomes. Such comments are often publicly accessible, particularly through platforms like the regulations.gov portal at the US federal level. InfluenceMap actively pursues Freedom of Information Act (FOIA) requests to access additional similar data sources.
>
> Evidence of top management messaging (D6) under various circumstances indicates potential efforts to influence policy through high-level communications directed at key influencers. The utilization of social media channels (D2) and, increasingly, paid targeted social media advertising (newly introduced as D8) are immensely important "external" lobbying tools for companies and their industry groups. These tools enable companies to shape the public narrative on climate change.
>
> It is worth noting that these data sources vary in availability across different regions and jurisdictions. Whenever InfluenceMap seeks to expand its system to encompass climate policy engagement in a new region, we conduct a comprehensive assessment, which includes stakeholder consultations to gauge data availability, and adapt our methodology accordingly.
>
> The LobbyMap system can identify inconsistencies in corporate communications across various data sources. For instance, it can pinpoint instances in which a company's high-level corporate disclosures or public relations messaging through social media differ from the messages conveyed directly to government officials.
>
> Our acknowledgment that the complete scope of corporate climate policy engagement might not be apparent in publicly available data sources is illustrated in Graphic 2. While the LobbyMap system does not encompass concealed or undisclosed information related to activities like private meetings and financial transactions, it does seek to provide a robust and statistically relevant account of corporate and industry group behavior by collecting and accessing the largest possible number of publicly accessible data points.

## Notes for the compiler

**Explicit parameters:**
- 7 active data sources (D1–D7 in the prose: Organizational Websites, Corporate Media, CDP, Direct Consultation, Reliable Media, Management Messaging, Financial Disclosures) plus 2 proposed (Lobbying Disclosures, Advertising). Note: the user's spec says "8 source classes" — the prose calls D8 "newly introduced" (paid social ads) so likely D1–D8 active when including ads.
- D3 (CDP) and D7 (Financial Disclosures) do not apply to industry associations (yields N/A cells in Stage 6).
- Source-selection criteria: publicly available, applicable to all entities, reliable.

**Implicit / ambiguous:**
- D-numbering (D1..D8) appears informally in the prose but Table 3 doesn't number rows. Compiler should canonicalize.
- No spec for ingestion frequency, document formats accepted, archival format (PDFs/text mentioned at line 336 in Stage 5 territory).
- "Reliable media" is not defined operationally.

**Cross-references:** Source weights (D4 and D6 highest) are defined in Stage 7 (org-score weighting).
