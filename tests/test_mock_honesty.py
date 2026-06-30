"""The mock LLM must never fabricate.

These pin the cardinal rule from the mock side: the tier-2 stubs emit only
documented negatives / 'unknown' with no invented sources, the verifier defaults
to refuted, and the generic fallback is an obvious stub rather than plausible
fake data.
"""
from __future__ import annotations

from app.runtime import llm_mock


def test_tier2_extract_never_asserts_true_and_cites_nothing():
    rows = llm_mock.mock_tier2_extract({"facility_id": "PO1000000320"})
    assert rows, "expected at least one feature row"
    for r in rows:
        assert r["asserted_present"] in {"false", "unknown"}   # never 'true'
        assert r["confidence"] == "low"
        assert r["evidence_urls"] == []                         # cites nothing


def test_tier2_verify_defaults_refuted_without_urls():
    assert llm_mock.mock_tier2_verify({"evidence_urls": []})["supported"] is False


def test_tier2_verify_refuted_even_with_urls():
    # the mock cannot re-fetch sources, so it must not claim support
    v = llm_mock.mock_tier2_verify({"evidence_urls": ["https://example.org/x"]})
    assert v["supported"] is False


def test_generic_mock_is_obvious_stub_not_fake_data():
    out = llm_mock.mock_generic("score the {x}", {"x": "thing"})
    assert out.get("_mock") is True
    # must NOT invent domain fields a downstream schema might trust
    assert not any(k in out for k in ("score", "value", "confidence", "evidence_urls"))
