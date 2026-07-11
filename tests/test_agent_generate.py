"""The headless generate-validate-retry loop (app.agent.agent.run_until_valid) and
the data-model block parser (app.compiler.data_model.parse_schema_blocks).

The loop is tested over a SCRIPTED run_turn (canned assistant texts) rather than the
real SDK engine, so no CLI subprocess is spawned. We assert the three behaviours that
matter: it returns the first valid artifact, it feeds each round's issues back into the
next prompt, and it raises (never returns an invalid artifact) once the round budget is
spent. Coroutines are driven with asyncio.run, mirroring tests/test_sdk_engine.py.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest

from app.agent.agent import run_until_valid
from app.compiler.data_model import parse_schema_blocks
from app.errors import GenerationError


def _scripted_run_turn(
    texts: list[str], captured_prompts: list[str]
) -> Callable[[str, str | None], Any]:
    """A run_turn that returns each canned text in turn, recording the prompt it was
    called with so a test can assert what got fed back between rounds."""
    outputs = iter(texts)

    async def run_turn(prompt: str, resume: str | None) -> tuple[str, str | None]:
        captured_prompts.append(prompt)
        return next(outputs), "sess-1"

    return run_turn


def _run(texts: list[str], validate: Callable[[str], list[str]], **kw: Any):
    """Drive run_until_valid with identity extraction over `texts`; return
    (result_or_exc, prompts_seen)."""
    prompts: list[str] = []
    coro = run_until_valid(
        _scripted_run_turn(texts, prompts),
        seed="SEED",
        extract=lambda text: text,
        validate=validate,
        max_rounds=kw.get("max_rounds", 3),
    )
    return asyncio.run(coro), prompts


def test_returns_first_valid_artifact_without_retrying() -> None:
    result, prompts = _run(["GOOD"], validate=lambda a: [])
    assert result == "GOOD"
    assert prompts == ["SEED"]  # exactly one turn, seeded with the document


def test_feeds_issues_back_then_returns_the_corrected_artifact() -> None:
    result, prompts = _run(
        ["BAD", "GOOD"],
        validate=lambda a: [] if a == "GOOD" else ["kind must be one of ..."],
    )
    assert result == "GOOD"
    assert len(prompts) == 2
    assert prompts[0] == "SEED"
    assert "kind must be one of ..." in prompts[1]  # the issue was kicked back


def test_raises_after_round_budget_never_returns_invalid() -> None:
    prompts: list[str] = []
    coro = run_until_valid(
        _scripted_run_turn(["BAD", "BAD", "BAD"], prompts),
        seed="SEED",
        extract=lambda text: text,
        validate=lambda a: ["always wrong"],
        max_rounds=3,
    )
    with pytest.raises(GenerationError) as exc_info:
        asyncio.run(coro)
    assert "always wrong" in str(exc_info.value)
    assert len(prompts) == 3  # spent exactly the budget


def test_parse_failure_is_fed_back_as_an_issue() -> None:
    prompts: list[str] = []

    def extract(text: str) -> str:
        if text == "GOOD":
            return text
        raise ValueError("bad format")

    coro = run_until_valid(
        _scripted_run_turn(["garbage", "GOOD"], prompts),
        seed="SEED",
        extract=extract,
        validate=lambda a: [],
        max_rounds=3,
    )
    assert asyncio.run(coro) == "GOOD"
    assert "could not parse" in prompts[1]
    assert "bad format" in prompts[1]


# ── parse_schema_blocks ─────────────────────────────────────────────────────────

_SCHEMA_TEXT = """\
Here are the tables.

```schema
{"name": "company", "kind": "input", "title": "Company", "columns": []}
```

```schema
{"name": "filing", "kind": "computed", "title": "Filing", "columns": []}
```

```python
print("not a schema")
```
"""


def test_parse_schema_blocks_extracts_only_schema_blocks() -> None:
    schemas = parse_schema_blocks(_SCHEMA_TEXT)
    assert [s["name"] for s in schemas] == ["company", "filing"]  # python block ignored


def test_parse_schema_blocks_raises_when_no_schema_block() -> None:
    with pytest.raises(ValueError, match="no ```schema blocks"):
        parse_schema_blocks("prose only, no fenced blocks")


def test_parse_schema_blocks_raises_on_malformed_json() -> None:
    text = "```schema\n{not valid json}\n```"
    with pytest.raises(ValueError):
        parse_schema_blocks(text)
