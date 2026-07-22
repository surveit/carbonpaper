import pytest
from app.core.errors import LLMError
from app.runtime.llm import render_prompt


def test_injects_present_column():
    assert render_prompt("hi {a}", {"a": "x"}) == "hi x"

def test_missing_column_raises():
    with pytest.raises(LLMError):
        render_prompt("hi {missing}", {})

def test_malformed_template_raises():
    with pytest.raises(LLMError):
        render_prompt("hi { b", {})

def test_escaped_braces_are_literal():
    assert render_prompt("literal {{x}}", {}) == "literal {x}"
