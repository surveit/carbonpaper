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


def test_extract_no_brace_at_all_reports_default_reason():
    # A parseable-but-non-dict JSON value with no '{' anywhere: json.loads
    # succeeds (so no decoder error is captured) but returns a list, and the
    # brace scan finds nothing to fall back to — the default "no JSON object
    # found" reason, not a decoder error string, must be on the first line.
    with pytest.raises(ValueError) as ei:
        compiler._extract_json_object("[1, 2, 3]")
    first_line = str(ei.value).splitlines()[0]
    assert "no JSON object found in the output" in first_line


def test_extract_balanced_scan_ignores_braces_inside_a_quoted_string():
    # The brace-depth scan must track quoted-string state so a '}' inside a
    # string value does not close the object early.
    text = 'prose {"note": "a } b", "x": 1} tail'
    assert compiler._extract_json_object(text) == {"note": "a } b", "x": 1}


def test_extract_balanced_scan_handles_escaped_quote_inside_a_string():
    # An escaped quote (\") inside a string value must not be read as the
    # string's closing quote, which would desync the brace-depth tracking.
    text = 'prose {"note": "a \\" b", "x": 1} tail'
    assert compiler._extract_json_object(text) == {"note": 'a " b', "x": 1}


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


def test_retry_feeds_validation_issues_into_next_prompt(monkeypatch):
    """First attempt parses but FAILS schema validation; second attempt validates.
    The retry prompt must carry the specific validation issues, and the clean
    result is returned."""
    calls: list[str] = []
    replies = iter([
        '{"stages": [{"id": "s1", "type": "bad"}]}',         # parses, invalid schema
        '{"stages": [{"id": "s1", "type": "input_data"}]}',  # parses + valid
    ])

    def fake_call_llm(prompt_text, model="sonnet", timeout_s=600):
        calls.append(prompt_text)
        return next(replies)

    validations = iter([["stage 's1': unknown type 'bad'"], []])
    monkeypatch.setattr(compiler, "call_llm", fake_call_llm)
    monkeypatch.setattr(compiler, "validate", lambda stages: next(validations))

    result = compiler.compile_methodology("some prose", "demo", max_attempts=3)

    assert len(calls) == 2
    retry_prompt = calls[1]
    assert "# RETRY 2" in retry_prompt
    assert "FAILED schema validation" in retry_prompt
    assert "unknown type 'bad'" in retry_prompt  # the concrete issue is handed back
    assert result["validation"] == []
    assert result["stages"] == [{"id": "s1", "type": "input_data"}]


def test_returns_least_invalid_result_when_never_clean(monkeypatch):
    """When no attempt validates cleanly, return the fewest-issues candidate with
    its issues surfaced — an invalid draft is reported, never raised or faked."""
    replies = iter([
        '{"stages": [{"id": "a"}]}',
        '{"stages": [{"id": "a"}, {"id": "b"}]}',
        '{"stages": [{"id": "a"}]}',
    ])
    issue_lists = iter([["i1", "i2"], ["i1"], ["i1", "i2"]])  # attempt 2 is least-invalid
    monkeypatch.setattr(compiler, "call_llm", lambda *a, **k: next(replies))
    monkeypatch.setattr(compiler, "validate", lambda stages: next(issue_lists))

    result = compiler.compile_methodology("some prose", "demo", max_attempts=3)

    assert result["validation"] == ["i1"]      # fewest-issues candidate kept
    assert len(result["stages"]) == 2
