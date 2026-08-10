"""The worked stage in the authoring prompts is a REAL stage: it parses with the
loader, and its code does what its corner cases say. A prompt example the write path
would refuse teaches a shape that cannot be authored."""
from __future__ import annotations

import json
import re

import pytest

from app.models import parse_stage
from app.tools.prompt_fragments import WORKED_STAGE_EXAMPLE


@pytest.fixture(scope="module")
def spec() -> dict:
    block = re.search(r"```json\n(.*?)\n```", WORKED_STAGE_EXAMPLE, re.S)
    assert block, "the example must carry one fenced json block"
    return json.loads(block.group(1))


def test_the_worked_example_parses_as_a_stage(spec: dict) -> None:
    stage = parse_stage(spec)
    assert stage.id == "normalize_spend"
    assert stage.signature.form == "extends"


def test_the_worked_example_carries_what_it_claims_to_show(spec: dict) -> None:
    # It is in the prompt to show summary + corner_cases beside the code they describe.
    block = spec["starlark"]
    assert block["summary"] and block["corner_cases"] and block["code"]


def test_the_example_code_matches_its_stated_corner_cases(spec: dict) -> None:
    # The two cases the example states, run through the real handler.
    import pandas as pd

    from app.models.errors import StepRefused
    from app.models.stage import StageType
    from app.runtime.stages import HANDLERS
    from conftest import rows_of, as_inputs, make_run_context

    stage = parse_stage(spec)
    handler = HANDLERS[StageType.starlark_row_function]
    blank = pd.DataFrame([{"filing_id": "F1", "reported_amount": None}])
    out = handler.execute(stage, as_inputs({"filings": blank}), make_run_context())
    assert out is not None and rows_of(out)["amount_usd"].isna().all()

    euros = pd.DataFrame([{"filing_id": "F2", "reported_amount": "\u20ac45,00"}])
    with pytest.raises(StepRefused):
        handler.execute(stage, as_inputs({"filings": euros}), make_run_context())


def test_the_example_reaches_both_authoring_prompts() -> None:
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT
    from app.mcp.server import INSTRUCTIONS

    assert WORKED_STAGE_EXAMPLE in EDITING_SYSTEM_PROMPT
    assert WORKED_STAGE_EXAMPLE in INSTRUCTIONS
