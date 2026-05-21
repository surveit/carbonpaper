---
stage_number: 9
stage_id: indirect_performance_band
stage_name: Indirect score + Performance Band
source_doc: examples/lobbymap/methodology_raw.txt
source_lines: [218, 219]
related_sections: ["§2.3 (Table 2 — metric definitions)", "§4.9 final paragraph", "Appendix B for finance variant"]
---

# Stage 9: Indirect score + Performance Band

## Prose excerpt

### Line 218 (§2.3 Table 2 — Performance Band definition; full row block; shared with Stages 7 and 8)

> **Performance Band (A+ to F)** — Performance Band is a full measure of a company's climate policy engagement, accounting for both its own engagement and that of its industry associations. For companies, the 'Organisation Score' and 'Relationship Score' are combined to result in a total score that places the company in a Performance Band. Industry associations do not have a 'Relationship Score', so the Performance Band for industry associations is made up of only the 'Organisation Score'.
>
> There are 16 Performance Bands from A+ (representing a total score from 95–100%) to E- (a score of 25–30%), with scores below 25% falling in the red "F" band. Grades from A+ to B (i.e., above 75%) indicate broad support for science-aligned policy for delivering the Paris Agreement's goal of delivering as close to 1.5°C warming as possible, with grades from D to F (i.e., below 50%) indicating increasingly obstructive climate policy engagement. If limited evidence has been collected on both a company's direct policy engagement (Organization Score) and industry association links (Relationship Score), the Performance Band is signified with an "n/a" (not available).

### Line 219 (footnote)

> *Relationship Scores are generated for companies only. The remaining three metrics are generated for both companies and industry associations.

### Line 218 (Relationship Score — repeated here as input to band; shared with Stage 8)

> **Relationship Score (0–100)*** — Relationship Score is a measure of how supportive or obstructive the company's industry associations are towards science-aligned climate policy... A score of 0 would indicate full opposition, and a score of 100 equates to full support... Scores between 50 and 75 indicate mixed engagement with such policy. If limited evidence has been collected on a company's industry association links, the Relationship Score is signified with an "n/a" (not available).

### Lines 290–291 (§4.1 — direct vs indirect framing; shared with Stages 3 and 4)

> Both assessments are run across our entire database of companies and industry associations. This allows for the creation of a series of core metrics that describe the positioning and intensity of each entity's "direct" climate policy engagement.
>
> Section 4.9 below further explains how the LobbyMap platform tracks relationships between companies and industry associations. This process, coupled with the alignment assessments of each industry association, enables a further set of calculations to determine the alignment of a company's "indirect" policy engagement via the industry associations of which it is a member.

## Notes for the compiler

**Explicit parameters:**
- Performance Band: 16 bands. A+ = 95–100%, descending in 5-point bins to E- = 25–30%; everything <25% is F (red).
- Inputs: company Performance Band combines Organization Score (direct, Stage 7) and Relationship Score (indirect, Stage 8). Industry-association Performance Band uses only Organization Score.
- Band semantics: A+ to B (>75%) = supportive; D to F (<50%) = obstructive; band is "n/a" if both inputs lack evidence.

**Implicit / ambiguous:**
- The combination formula between Organization Score and Relationship Score is **not given**. Prose says they are "combined to result in a total score" — likely a weighted average, but the weights are unspecified. Compiler will need to flag this.
- Band 5-point bin layout: 16 bands from A+ (95–100) to E- (25–30) implies bins of 5%, but A through E- mapping (A+, A, A-, B+, B, B-, ... E-) needs explicit enumeration; prose only anchors A+ and E-.
- "F" band is single bin (0–25%), wider than the rest.
- N/A rule: if direct OR indirect is n/a, behavior is unstated (prose only addresses the case where *both* are n/a).
- Engagement Intensity does not feed Performance Band — but Engagement Intensity <5 already suppresses Org Score (Stage 7), which would propagate to Performance Band as n/a.

**Cross-references:** Org Score from Stage 7 + Relationship Score from Stage 8. Output is the final user-facing grade. The "Performance Band" name is informally a final reduce step; in the user's spec this stage is the indirect-score *plus* band computation.
