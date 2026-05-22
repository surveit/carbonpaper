"""
Deterministic mock LLM for the prototype.

Given a stage's input row and prompt template, returns plausible structured
output WITHOUT calling a real model. The mock uses keyword pattern matching
so the demo produces meaningful (not random) results.

This is good enough to:
  - run the pipeline end-to-end without API keys
  - exercise schema validation between stages
  - show what the runtime LOOKS like producing rows
  - let the user see the data flow with real-ish content

Two stages have specialised behaviour: evidence_extraction and benchmark_scoring.
Other llm_transform stages get a generic best-effort fallback.
"""

from __future__ import annotations

import json
import re
from typing import Any


# ─── Evidence extraction ─────────────────────────────────────────────────────

# Map keyword regex → policy_query and inferred stance. (LobbyMap / climate.)
QUERY_KEYWORD_TABLE: list[tuple[str, str, str]] = [
    # (regex, query_id, default_stance)
    (r"\bcarbon\s+(tax|pric(ing|e))\b", "Q5", "supports"),
    (r"\bcarbon\s+border\s+adjustment\b|\bCBAM\b", "Q5", "supports"),
    (r"\bcap\s+and\s+trade\b|\bemissions?\s+trading\b", "Q6", "supports"),
    (r"\brenewable\s+(energy|electricity|power)\b|\bsolar\b|\bwind\b", "Q7", "supports"),
    (r"\benergy\s+efficiency\b", "Q8", "supports"),
    (r"\bfossil\s+fuel\s+subsid(y|ies)\b", "Q9", "opposes"),
    (r"\b2035\s+(ICE|ban|phase[- ]?out)\b|\bICE\s+phase[- ]?out\b", "Q10", "opposes"),
    (r"\bmethane\b", "Q11", "supports"),
    (r"\bIPCC\b|\b1\.?5\s*°?C\b|\bnet[- ]?zero\b", "Q1", "supports"),
    (r"\bScope\s+[123]\b|\bGHG\s+emissions\b|\bgreenhouse\s+gas\b", "Q3", "supports"),
    (r"\benergy\s+transition\b|\bjust\s+transition\b", "Q2", "supports"),
    (r"\bclimate\s+disclosure\b|\bclimate\s+risk\b", "Q4", "supports"),
    (r"\blobby(ing)?\b|\btransparenc[yi]e?\b", "Q12", "supports"),
]

# CongressWatch healthcare queries (active when entity_id starts with 'M:').
CW_QUERY_TABLE: list[tuple[str, str, str]] = [
    (r"\b(ACA|Affordable\s+Care\s+Act|enhanced\s+premium|premium\s+tax\s+credit)\b",
     "Q1_aca_premium_credits", "supports"),
    (r"\b(Medicare\s+(drug|prescription)|drug\s+price\s+negotiation|Inflation\s+Reduction\s+Act|IRA\s+drug)\b",
     "Q2_medicare_drug_pricing", "supports"),
    (r"\b(Medicaid(\s+funding)?|work\s+requirement)\b",
     "Q3_medicaid_funding", "supports"),
    (r"\b(drug\s+import(ation)?|Canadian\s+drugs?|importation\s+pathway)\b",
     "Q4_drug_imports", "supports"),
    (r"\b(prior\s+auth(orization)?|insurer\s+deni(al|es)|network\s+adequacy|surprise\s+billing)\b",
     "Q5_insurer_regulation", "supports"),
]

# Member-specific stance hints common in political speech.
CW_OPPOSE_HINTS = re.compile(
    r"\b(let\s+(it|them)\s+expire|cut\s+|repeal|roll\s+back|sunset|defund|block|"
    r"reject\s+|stop\s+the|wasteful|fraud|abuse|out\s+of\s+control|"
    r"socialized|government\s+takeover|killed|burden)\b",
    re.IGNORECASE,
)
CW_SUPPORT_HINTS = re.compile(
    r"\b(extend|protect|defend|expand|strengthen|preserve|fight\s+for|fighting\s+for|"
    r"committed\s+to|will\s+(vote|fight)\s+to|introduce(d)?|signed\s+on|"
    r"co-?sponsor|championing)\b",
    re.IGNORECASE,
)

# Keywords that flip stance toward opposition.
OPPOSITION_HINTS = re.compile(
    r"\b(oppose|against|reject|undermine|delay|weaken|excessive|cannot support|"
    r"hurt|harm|constrain|aggressive policies|excessive cost|not support)\b",
    re.IGNORECASE,
)
SUPPORT_HINTS = re.compile(
    r"\b(support|endorse|advocate|champion|commit|pursue|backing)\b",
    re.IGNORECASE,
)
HEDGE_HINTS = re.compile(
    r"\b(caution|concerned|all[- ]of[- ]the[- ]above|balanced|multi-pathway|"
    r"flexibility|level playing field|pick winners)\b",
    re.IGNORECASE,
)


def mock_evidence_extraction(input_row: dict[str, Any]) -> list[dict[str, Any]]:
    """For evidence_extraction: extract policy-relevant snippets.

    Switches between LobbyMap (climate) and CongressWatch (healthcare)
    keyword tables based on entity_id prefix. Members of Congress use the
    'M:' prefix; companies/associations use 'C:'/'A:'."""
    entity_id = str(input_row.get("entity_id") or "")
    if entity_id.startswith("M:"):
        table = CW_QUERY_TABLE
        oppose_hints = CW_OPPOSE_HINTS
        support_hints = CW_SUPPORT_HINTS
    else:
        table = QUERY_KEYWORD_TABLE
        oppose_hints = OPPOSITION_HINTS
        support_hints = SUPPORT_HINTS

    body = input_row.get("body", "") or ""
    sentences = _split_sentences(body)
    out: list[dict[str, Any]] = []
    seen_queries = set()
    for idx, sent in enumerate(sentences):
        for pattern, query_id, default_stance in table:
            if re.search(pattern, sent, re.IGNORECASE):
                stance = default_stance
                if oppose_hints.search(sent):
                    stance = "opposes"
                elif HEDGE_HINTS.search(sent) and not support_hints.search(sent):
                    stance = "mixed"
                elif support_hints.search(sent):
                    stance = "supports"
                key = (query_id, sent[:60])
                if key in seen_queries:
                    continue
                seen_queries.add(key)
                out.append({
                    "query_id": query_id,
                    "quote": sent.strip(),
                    "stance_summary": stance,
                    "confidence": 0.78 + 0.05 * (1 if support_hints.search(sent) or oppose_hints.search(sent) else 0),
                    "rationale": f"matched pattern for {query_id}; stance from {stance} keywords",
                    "char_offset_start": body.find(sent),
                    "char_offset_end": body.find(sent) + len(sent),
                })
                break  # one query per sentence
    return out


# ─── Benchmark scoring ───────────────────────────────────────────────────────

def mock_benchmark_scoring(input_row: dict[str, Any]) -> dict[str, Any]:
    """For benchmark_scoring: produce a -2..+2 score from quote vs benchmark."""
    quote = input_row.get("quote", "") or ""
    benchmark = input_row.get("benchmark_text", "") or ""
    kind = input_row.get("kind", "science_based")

    has_strong_support = bool(re.search(r"\b(strongly support|fully endorse|champion|commit to|will deliver|driven to|fight to extend|protect|defend)\b", quote, re.IGNORECASE))
    has_support = bool(SUPPORT_HINTS.search(quote) or CW_SUPPORT_HINTS.search(quote))
    has_oppose = bool(OPPOSITION_HINTS.search(quote) or CW_OPPOSE_HINTS.search(quote))
    has_strong_oppose = bool(re.search(r"\b(strongly oppose|reject|fundamentally disagree|cannot support|will not|repeal|let.*expire|defund)\b", quote, re.IGNORECASE))
    has_hedge = bool(HEDGE_HINTS.search(quote))

    if has_strong_oppose:
        score = -2
        reasoning = "Quote uses unambiguous oppositional language; position contradicts the benchmark."
    elif has_oppose and not has_support:
        score = -1
        reasoning = "Quote opposes the benchmark, with caveats short of full contradiction."
    elif has_strong_support and not has_hedge:
        score = 2
        reasoning = "Quote contains strong, detailed alignment with the benchmark; concrete commitment."
    elif has_support and has_hedge:
        score = 0
        reasoning = "Quote expresses support but with significant caveats / hedges that obscure the position."
    elif has_support:
        score = 1
        reasoning = "Quote shows broad alignment with the benchmark; lacks the specifics of a strong-support tier."
    elif has_hedge:
        score = 0
        reasoning = "Quote is hedged; position relative to the benchmark is unclear."
    else:
        score = 0
        reasoning = "Quote does not clearly take a position relative to the benchmark."

    # Government-policy benchmarks use slightly different rubric semantics
    if kind == "government_policy" and score == 1 and has_hedge:
        score = -1
        reasoning = "Stated support carries weakening conditions characteristic of policy opposition under Table 6."

    return {
        "score": score,
        "reasoning": reasoning,
        "precedence_failure": False,
    }


# ─── Generic fallback ────────────────────────────────────────────────────────

def mock_generic(prompt_template: str, input_row: dict[str, Any]) -> dict[str, Any]:
    """For any llm_transform we don't have a specialised mock for."""
    return {
        "_mock": True,
        "_prompt_truncated": prompt_template[:120],
        "_inputs_truncated": {k: str(v)[:80] for k, v in input_row.items()},
    }


# ─── Helpers ─────────────────────────────────────────────────────────────────

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _split_sentences(text: str) -> list[str]:
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_BREAK.split(text) if s.strip()]


# ─── Dispatcher ──────────────────────────────────────────────────────────────

def mock_llm_call(stage_id: str, llm_config: dict[str, Any], input_row: dict[str, Any]) -> Any:
    """Return either a single output dict or a list of output dicts."""
    if stage_id == "evidence_extraction":
        return mock_evidence_extraction(input_row)
    if stage_id == "benchmark_scoring":
        return mock_benchmark_scoring(input_row)
    return mock_generic(llm_config.get("prompt_template", ""), input_row)
