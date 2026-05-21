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

# Map keyword regex → policy_query and inferred stance.
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
    """For evidence_extraction: extract policy-relevant snippets."""
    body = input_row.get("body", "") or ""
    sentences = _split_sentences(body)
    out: list[dict[str, Any]] = []
    seen_queries = set()
    for idx, sent in enumerate(sentences):
        for pattern, query_id, default_stance in QUERY_KEYWORD_TABLE:
            if re.search(pattern, sent, re.IGNORECASE):
                stance = default_stance
                if OPPOSITION_HINTS.search(sent):
                    stance = "opposes"
                elif HEDGE_HINTS.search(sent) and not SUPPORT_HINTS.search(sent):
                    stance = "mixed"
                elif SUPPORT_HINTS.search(sent):
                    stance = "supports"
                key = (query_id, sent[:60])
                if key in seen_queries:
                    continue
                seen_queries.add(key)
                out.append({
                    "query_id": query_id,
                    "quote": sent.strip(),
                    "stance_summary": stance,
                    "confidence": 0.78 + 0.05 * (1 if SUPPORT_HINTS.search(sent) or OPPOSITION_HINTS.search(sent) else 0),
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

    has_strong_support = bool(re.search(r"\b(strongly support|fully endorse|champion|commit to|will deliver|driven to)\b", quote, re.IGNORECASE))
    has_support = bool(SUPPORT_HINTS.search(quote))
    has_oppose = bool(OPPOSITION_HINTS.search(quote))
    has_strong_oppose = bool(re.search(r"\b(strongly oppose|reject|fundamentally disagree|cannot support|will not)\b", quote, re.IGNORECASE))
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
