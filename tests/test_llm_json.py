"""JSON-recovery helpers in app/runtime/llm.py.

These guard the no-fabrication contract from the *parsing* side: on unparseable
output we return the raw text (never a made-up value), and we can recover a final
JSON answer that a research-mode agent prefixed with narration.
"""
from __future__ import annotations

from app.runtime import llm


def test_parse_plain_json_object():
    assert llm._parse_text_result('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_parse_strips_code_fences():
    fenced = "```json\n{\"a\": 1}\n```"
    assert llm._parse_text_result(fenced) == {"a": 1}


def test_parse_non_string_passthrough():
    payload = {"already": "parsed"}
    assert llm._parse_text_result(payload) is payload


def test_parse_freeform_text_returns_raw():
    assert llm._parse_text_result("just prose, no json here") == "just prose, no json here"


def test_parse_prose_with_trailing_json_recovers_it():
    # research mode narrates, then emits JSON — recover the JSON, not the prose
    s = 'Searched 3 sources. {"supported": false, "evidence_urls": []}'
    assert llm._parse_text_result(s) == {"supported": False, "evidence_urls": []}


def test_extract_last_json_from_prose():
    s = 'I found nothing solid. Final: {"feature": "x", "evidence_urls": []}'
    assert llm._extract_last_json(s) == {"feature": "x", "evidence_urls": []}


def test_extract_last_json_picks_last_value():
    assert llm._extract_last_json('first [1, 2, 3] then {"k": "v"}') == {"k": "v"}


def test_extract_last_json_none_when_absent():
    assert llm._extract_last_json("no json at all") is None
