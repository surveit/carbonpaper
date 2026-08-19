"""An exhausted account stops the stage; it is never retried, never tagged per row.

A 429 says the ACCOUNT is out, so the next attempt asks the same exhausted
account the same question. Both supervisors let it through rather than turn one
account-level fact into thousands of row errors.
"""
from __future__ import annotations

import pandas as pd
import pytest
from claude_agent_sdk import ResultMessage

import app.core.agent.sdk_engine as sdk_engine
from app.core.agent.errors import AccountLimitReached
from app.models import parse_stage
from app.models.stage import StageType
from app.runtime import llm as runtime_llm
from app.runtime.stages import HANDLERS
from conftest import as_inputs, make_run_context, place_stage

# More rows than the driver's parallelism, so "it stopped" is distinguishable
# from "it happened to finish": 12 rows x 4 attempts is 48 calls unguarded.
_ROWS = pd.DataFrame({"post_id": [f"p{i}" for i in range(12)],
                      "text": [f"t{i}" for i in range(12)]})

_LIMIT_TEXT = "You've hit your session limit · resets 5:40pm (UTC)"


def _limit_result() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=True,
        num_turns=1, session_id="s1", result=_LIMIT_TEXT, api_error_status=429,
        terminal_reason="api_error",
    )


@pytest.fixture
def exhausted_account(monkeypatch):
    """Every model call answers with the CLI's terminal 429. Counts the calls."""
    calls = {"n": 0}

    async def fake_query(*, prompt, options):
        calls["n"] += 1
        yield _limit_result()

    monkeypatch.setattr(sdk_engine, "query", fake_query)
    monkeypatch.setattr(runtime_llm, "require_agent_backend", lambda: None)
    return calls


def _stage(batch_size: int):
    return parse_stage({
        "id": "classify", "description": "Classify", "type": "llm_transform",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": [
                {"name": "text", "type": "str", "nullable": True}]}],
            "adds": [{"name": "label", "type": "str", "nullable": True}]},
        # max_retries 3 is the point: without the fix this is 4 calls per row.
        "llm": {"prompt_data_template": "classify {text}", "max_retries": 3,
                "batch_size": batch_size},
    })


def _placed(batch_size: int):
    return place_stage(_stage(batch_size), load={"columns": [
        {"name": "post_id", "type": "str", "nullable": True},
        {"name": "text", "type": "str", "nullable": True}]})


def test_a_row_stage_stops_on_the_first_exhausted_call(exhausted_account):
    with pytest.raises(AccountLimitReached) as caught:
        HANDLERS[StageType.llm_transform].execute(
            _placed(batch_size=1), as_inputs({"load": _ROWS.copy()}), make_run_context())
    # The CLI's own words, because only they say WHICH allowance and when it resets.
    assert _LIMIT_TEXT in str(caught.value)
    # Rows already dispatched cannot be un-launched, so the bound is the number
    # of rows, never the 12 x 4 attempts an unguarded retry loop would spend.
    assert exhausted_account["n"] <= len(_ROWS)


def test_a_batched_stage_stops_rather_than_failing_every_chunk(exhausted_account):
    with pytest.raises(AccountLimitReached):
        HANDLERS[StageType.llm_transform].execute(
            _placed(batch_size=2), as_inputs({"load": _ROWS.copy()}), make_run_context())
    # Six chunks of two; the ones still queued behind the first failure never ran.
    assert exhausted_account["n"] < len(_ROWS) // 2
