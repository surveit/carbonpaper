import re

import pytest
from pydantic import ValidationError

from app.models.schema import Column, TableSchema
from app.models.stage_base import StageInput, StageType
from app.models.stages.node_types import NODE_TYPES
from app.models.stages.starlark import StarlarkFunction, StarlarkRowFunctionStage

GOOD = "def transform(row):\n    return {'n': row['n'] + 1}\n"

_SCHEMA = TableSchema(columns=[Column(name="n", type="int", nullable=False)])
_INPUT = StageInput(id="load", schema=_SCHEMA)


def _stage(**overrides):
    fields = dict(
        id="bump", name="Bump n", type=StageType.starlark_row_function,
        inputs=[_INPUT], output_schema=_SCHEMA,
        starlark=StarlarkFunction(code=GOOD),
    )
    fields.update(overrides)
    return StarlarkRowFunctionStage(**fields)


def test_validate_docstring_says_it_executes_the_code():
    # Regression: `validate_starlark_function_code` compiles via `starlark.eval`,
    # which runs `code`'s top-level statements — describing this as a mere
    # binding check without saying it executes would be misleading.
    from app.models.stages.starlark import validate_starlark_function_code

    assert "execut" in (validate_starlark_function_code.__doc__ or "").lower()


def test_a_list_holding_a_function_is_rejected_at_save():
    # Regression: `module[name]` raises identically whether `name` is directly
    # a function or a container merely HOLDING one (serde walks the whole
    # value graph), so the ownership check alone once let this save.
    code = "def f(row):\n    return row\ntransform = [f]\n"
    with pytest.raises(ValueError):
        StarlarkFunction(code=code)


def test_a_dict_holding_a_function_is_rejected_at_save():
    code = "def f(row):\n    return row\ntransform = {'f': f}\n"
    with pytest.raises(ValueError):
        StarlarkFunction(code=code)


def test_a_lambda_bound_to_the_wanted_name_is_accepted_at_save():
    # Deliberate: a lambda IS a function, so it is accepted like a `def`.
    code = "transform = lambda row: row\n"
    assert StarlarkFunction(code=code).code == code


def test_accepts_source_defining_transform():
    assert StarlarkFunction(code=GOOD).code == GOOD


def test_rejects_source_that_does_not_parse():
    with pytest.raises(ValueError):
        StarlarkFunction(code="def transform(row)\n    return row\n")


def test_rejects_source_defining_no_transform():
    with pytest.raises(ValueError) as err:
        StarlarkFunction(code="x = 1\n")
    assert "transform" in str(err.value)


def test_rejects_source_binding_the_name_to_a_non_function():
    with pytest.raises(ValueError):
        StarlarkFunction(code="transform = 5\n")


def test_honours_a_named_function():
    source = "def relabel(row):\n    return row\n"
    assert StarlarkFunction(code=source, function="relabel").function == "relabel"
    with pytest.raises(ValueError):
        StarlarkFunction(code=source, function="absent")


@pytest.mark.parametrize("name", ["len", "dict", "fail", "str", "type", "sorted", "range"])
def test_naming_a_standard_global_as_function_is_rejected_not_saved(name):
    # Regression: the probe once evaluated `type(<name>)` against the standard
    # globals, so a name the standard library provides (never bound by `code`
    # itself) read as bound, and a stage saved that defines nothing.
    with pytest.raises(ValueError):
        StarlarkFunction(code="x = 1\n", function=name)


def test_source_calling_refuse_validates():
    # `refuse` is injected at validation time too, so validation and execution
    # compile in the same shape.
    assert StarlarkFunction(code="def transform(row):\n    refuse('no')\n").code


def test_a_typo_d_helper_name_is_caught_at_write_time():
    # A call to an undefined helper fails Starlark's static free-variable
    # resolution at load, so a typo is rejected when the stage is SAVED,
    # not on the first row the runtime processes.
    with pytest.raises(ValueError):
        StarlarkFunction(code="def transform(row):\n    return helepr(row)\n")


def test_non_identifier_function_name_is_rejected_at_save_time_not_execution():
    with pytest.raises(ValidationError):
        StarlarkFunction(code=GOOD, function="refuse('x')) or (1")


@pytest.mark.parametrize("source", [
    "import os\ndef transform(row):\n    return row\n",
    "def transform(row):\n    while True:\n        pass\n",
    "def transform(row):\n    try:\n        return row\n    except:\n        return row\n",
    "class Thing:\n    pass\ndef transform(row):\n    return row\n",
])
def test_rejects_python_constructs_starlark_does_not_have(source):
    with pytest.raises(ValueError):
        StarlarkFunction(code=source)


def test_a_self_terminating_recursive_function_saves():
    # Regression: recursion is NOT in the reject list above — a self-terminating
    # recursive function must validate at save time, since it runs fine (see
    # tests/runtime/test_starlark_code.py for the runtime-level pin, including
    # the call-stack limit that bounds an UNbounded recursion instead).
    source = (
        "def fact(n):\n    if n <= 1:\n        return 1\n    return n * fact(n - 1)\n"
        "def transform(row):\n    return {'r': fact(row['n'])}\n"
    )
    assert StarlarkFunction(code=source).code == source


def test_stage_requires_exactly_one_input():
    with pytest.raises(ValidationError):
        _stage(inputs=[_INPUT, StageInput(id="load2", schema=_SCHEMA)])
    with pytest.raises(ValidationError):
        _stage(inputs=[])


def test_stage_carries_runnable_tests():
    assert StarlarkRowFunctionStage.CARRIES_RUNNABLE_TESTS is True


def test_fingerprint_blocks_names_the_starlark_block():
    stage = _stage()
    assert stage.fingerprint_blocks() == {"starlark": stage.starlark}


def test_find_authored_code_block_returns_the_config():
    stage = _stage()
    assert stage.find_authored_code_block() is stage.starlark


# The row-merge idiom both authoring-guidance passages teach, pulled out of the real
# text (never duplicated) so a future edit to either passage is checked against the
# actual interpreter rather than trusted on its word. The right-hand side of an
# illustrative `name=value` pair is prose ("value"), not a bound Starlark name, so
# it is swapped for a literal before compiling — the CALL SHAPE quoted in the prose
# is what is under test, not its placeholder wording.
_ROW_MERGE_IDIOM = re.compile(r"`(return\s+[^`]+)`")


def _compilable_idioms_from(description: str) -> list[str]:
    matches = _ROW_MERGE_IDIOM.findall(description)
    assert matches, (
        "expected a `return ...` row-merge idiom quoted in this authoring-guidance "
        f"text, found none: {description!r}"
    )
    return [re.sub(r"=\s*\w+", "=1", match) for match in matches]


@pytest.mark.parametrize("description", [
    StarlarkFunction.model_fields["code"].description,
    NODE_TYPES["starlark_row_function"]["notes"],
], ids=["StarlarkFunction.code field description", "NODE_TYPES notes"])
def test_authoring_guidance_teaches_a_row_merge_idiom_the_parser_accepts(description):
    # Regression: an earlier revision of both passages told an author (including the
    # compiler agent, which reads this exact text) to write `return {**row, ...}` —
    # syntax starlark-pyo3's parser rejects outright, so every stage authored by
    # following the guidance would fail to save. Checks EVERY quoted `return ...`
    # idiom in the passage, not just the first — a passage may quote more than one.
    for idiom in _compilable_idioms_from(description):
        code = f"def transform(row):\n    {idiom}\n"
        assert StarlarkFunction(code=code).code == code
