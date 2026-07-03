"""Offline tests for the compile mechanism (no LLM / no CLI).

Covers JSON extraction from model output and the retry loop's corrective
feedback: a parse failure must feed the PRECISE decoder reason back into the next
attempt's prompt, without echoing the model's broken output back.
"""

from __future__ import annotations

import pytest

from app.compiler import compiler


def test_extract_plain_json():
    assert compiler._extract_json_object('{"stages": [1]}') == {"stages": [1]}


def test_extract_fenced_json():
    text = 'here you go:\n```json\n{"a": 1}\n```\nthanks'
    assert compiler._extract_json_object(text) == {"a": 1}


def test_extract_embedded_json_object():
    text = 'prose before {"a": {"b": 2}} prose after'
    assert compiler._extract_json_object(text) == {"a": {"b": 2}}


def test_extract_empty_raises():
    with pytest.raises(ValueError, match="empty"):
        compiler._extract_json_object("   ")


def test_extract_failure_reports_decoder_reason_on_first_line():
    # A truncated object: the decoder reason should be on the FIRST line so the
    # retry loop can hand it back; the raw snippet is on later lines only.
    bad = '{"stages": [1, 2,'
    with pytest.raises(ValueError) as ei:
        compiler._extract_json_object(bad)
    first_line = str(ei.value).splitlines()[0]
    assert "Could not parse JSON" in first_line
    # The concrete json decoder reason is carried, not a generic message.
    assert "line" in first_line and "column" in first_line
    # The raw output is NOT part of the first line (no anchoring the re-emit).
    assert "stages" not in first_line


def test_retry_feeds_decoder_reason_into_next_prompt(monkeypatch):
    """First attempt returns unparseable JSON, second returns a valid DAG. The
    second prompt must contain the decoder reason from the first failure."""
    calls: list[str] = []
    replies = iter([
        '{"stages": [1, 2,',                                  # malformed → triggers retry
        '{"stages": [{"id": "s1", "type": "input_data"}]}',  # valid
    ])

    def fake_call_llm(prompt_text, model="sonnet", timeout_s=600):
        calls.append(prompt_text)
        return next(replies)

    monkeypatch.setattr(compiler, "call_llm", fake_call_llm)
    # validate() is exercised elsewhere; here we only care about the retry wiring.
    monkeypatch.setattr(compiler, "validate", lambda stages: [])

    result = compiler.compile_methodology("some prose", "demo", max_attempts=3)

    assert len(calls) == 2, "should have re-asked exactly once after the parse failure"
    retry_prompt = calls[1]
    assert "# RETRY 2" in retry_prompt
    assert "Reason:" in retry_prompt
    # The precise decoder reason is present; the broken output is not echoed back.
    assert "line" in retry_prompt and "column" in retry_prompt
    assert result["stages"] == [{"id": "s1", "type": "input_data"}]
