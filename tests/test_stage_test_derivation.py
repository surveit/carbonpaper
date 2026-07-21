"""The deriver bridge: task assembly is code-blind and schema-grounded. The repair bridge is
its mirror: it DOES see the code, but its answer schema carries no test-editing lever."""
import pytest

from app.compiler.stage_tests import (
    RepairedStageCode,
    build_stage_test_deriver,
    build_stage_test_repair_agent,
    render_derivation_task,
    render_repair_task,
)
from app.core.models import Stage

_CODE = "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"


def _python_stage() -> Stage:
    return Stage.model_validate({
        "id": "double", "name": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": {"columns": [
            {"name": "amount", "type": "float", "nullable": False},
        ]}}],
        "function": {"kind": "inline", "code": _CODE},
        "output_schema": {"columns": [
            {"name": "amount", "type": "float", "nullable": False},
            {"name": "doubled", "type": "float", "nullable": False},
        ]},
    })


def test_task_contains_document_schemas_and_stage_meta():
    task = render_derivation_task("----doc text----", _python_stage())
    assert "----doc text----" in task
    assert "Double" in task           # stage name rendered
    assert "double" in task           # stage id rendered
    assert "doubled" in task          # output schema rendered
    assert "load" in task             # input id rendered


def test_task_never_contains_the_stage_code():
    task = render_derivation_task("doc", _python_stage())
    assert "def transform" not in task
    assert _CODE not in task


def test_task_never_contains_existing_tests():
    stage = _python_stage()
    stage = Stage.model_validate({**stage.model_dump(by_alias=True, exclude_none=True),
        "tests": [{"name": "stale_case",
                   "inputs": {"load": [{"amount": 1.0}]},
                   "expected": [{"amount": 1.0, "doubled": 2.0}]}]})
    task = render_derivation_task("doc", stage)
    assert "stale_case" not in task


def test_deriver_rejects_non_python_stages():
    bad = Stage.model_validate({
        "id": "pub", "name": "Publish", "type": "publish",
        "inputs": [{"id": "double"}],
        "function": {"kind": "inline", "code": "def transform(df, output_dir):\n    return df\n"},
        "publish": {},
    })
    with pytest.raises(ValueError, match="python transforms"):
        build_stage_test_deriver("doc", bad)


def test_deriver_target_schema_is_stage_bound():
    agent = build_stage_test_deriver("doc", _python_stage())
    with pytest.raises(Exception, match="declared inputs"):
        agent._target_schema.model_validate({"tests": [{
            "name": "x", "inputs": {"ghost": [{"amount": 1.0}]},
            "expected": [{"amount": 1.0, "doubled": 2.0}]}]})


# ── the code-repair bridge ────────────────────────────────────────────────────────────

def test_repair_agent_answer_schema_is_code_only():
    """The repair agent's ONLY output field is `code` — it structurally cannot edit the tests,
    which is the whole invariant (a red test is fixed by fixing code, never by rewriting it)."""
    agent = build_stage_test_repair_agent(_python_stage(), "some failures")
    assert agent._target_schema is RepairedStageCode
    assert set(RepairedStageCode.model_fields) == {"code"}


def test_repair_task_shows_code_and_failures_but_not_the_methodology():
    task = render_repair_task(_python_stage(), "FAILREPORT: row 0 differs")
    assert "def transform" in task        # the code IS shown to the repairer
    assert "FAILREPORT" in task           # and the failing-test report
    assert "double" in task               # stage identity


def test_repair_agent_rejects_non_python_stage():
    pub = Stage.model_validate({
        "id": "pub", "name": "Publish", "type": "publish",
        "inputs": [{"id": "double"}],
        "function": {"kind": "inline", "code": "def transform(df, output_dir):\n    return df\n"},
        "publish": {},
    })
    with pytest.raises(ValueError, match="python transforms"):
        build_stage_test_repair_agent(pub, "failures")
