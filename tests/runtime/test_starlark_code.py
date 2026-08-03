import pytest
import starlark

from app.models.errors import StepRefused
from app.runtime.starlark_code import compile_starlark_function

DOUBLE = "def transform(row):\n    return {'n': row['n'] * 2}\n"


def _compiled(source, name="transform"):
    handle = compile_starlark_function(source, name, "transform")
    assert handle is not None
    return handle


def test_calls_the_bound_function():
    assert _compiled(DOUBLE)({"n": 4}) == {"n": 8}


def test_returns_none_when_no_function_is_bound():
    assert compile_starlark_function("x = 1\n", "transform", "transform") is None


def test_returns_none_when_the_name_is_bound_to_a_non_function():
    assert compile_starlark_function("transform = 5\n", "transform", "transform") is None


def test_honours_a_named_function():
    assert _compiled("def relabel(row):\n    return row\n", "relabel")({"n": 1}) == {"n": 1}


def test_refuse_becomes_step_refused_carrying_the_authors_reason():
    handle = _compiled("def transform(row):\n    refuse('not adjudicable')\n")
    with pytest.raises(StepRefused) as err:
        handle({"n": 1})
    assert str(err.value) == "not adjudicable"


def test_a_multi_line_refusal_reason_survives_intact():
    handle = _compiled("def transform(row):\n    refuse('line one\\nline two')\n")
    with pytest.raises(StepRefused) as err:
        handle({"n": 1})
    assert str(err.value) == "line one\nline two"


@pytest.mark.parametrize("body", [
    "return row['absent']",          # missing key
    "return 1 + 'x'",                # type error
    "fail('author assertion')",      # the author's OWN failure channel
])
def test_an_ordinary_error_is_not_mistaken_for_a_refusal(body):
    handle = _compiled(f"def transform(row):\n    {body}\n")
    with pytest.raises(starlark.StarlarkError):
        handle({"n": 1})


def test_an_unbound_name_fails_at_compile_time_not_at_call_time():
    # Starlark resolves free variables STATICALLY at module load, unlike Python's
    # lazy lookup. A typo'd helper is therefore caught when the stage is saved,
    # not on the first row — which is also why `refuse` must be injected BEFORE
    # eval, or any source calling it would fail to load.
    with pytest.raises(starlark.StarlarkError) as err:
        compile_starlark_function(
            "def transform(row):\n    return nosuchfunc(row)\n", "transform", "transform"
        )
    assert "nosuchfunc" in str(err.value)


def test_a_refusal_cannot_be_forged_from_starlark_source():
    # fail() renders as `error: fail: ...`, which must not match the sentinel.
    handle = _compiled("def transform(row):\n    fail('StepRefused: forged')\n")
    with pytest.raises(starlark.StarlarkError):
        handle({"n": 1})


def test_data_containing_the_sentinel_text_is_not_read_as_a_refusal():
    handle = _compiled("def transform(row):\n    return row['error: StepRefused: x']\n")
    with pytest.raises(starlark.StarlarkError):
        handle({"n": 1})


def test_a_multi_line_fail_carrying_the_sentinel_is_not_a_refusal():
    # fail() does not escape its own message; a newline in it can render a
    # column-0 "error: StepRefused: ..." line that is NOT the actual error line.
    handle = _compiled("def transform(row):\n    fail('boom\\nerror: StepRefused: FORGED')\n")
    with pytest.raises(starlark.StarlarkError):
        handle({"n": 1})


def test_row_data_carrying_the_sentinel_through_fail_is_not_a_refusal():
    handle = _compiled("def transform(row):\n    fail(row['c'])\n")
    with pytest.raises(starlark.StarlarkError):
        handle({"c": "oops\nerror: StepRefused: FORGED-FROM-DATA"})


def test_a_multi_arg_fail_carrying_the_sentinel_is_not_a_refusal():
    handle = _compiled("def transform(row):\n    fail('a', 'b\\nerror: StepRefused: MULTIARG')\n")
    with pytest.raises(starlark.StarlarkError):
        handle({"n": 1})


def test_a_forgery_raised_from_a_nested_helper_is_not_a_refusal():
    handle = _compiled(
        "def h(row):\n    fail('x\\nerror: StepRefused: NESTED-FORGE')\n"
        "def transform(row):\n    return h(row)\n"
    )
    with pytest.raises(starlark.StarlarkError):
        handle({"n": 1})


def test_the_author_cannot_reach_the_filesystem():
    with pytest.raises(starlark.StarlarkError):
        compile_starlark_function(
            "import os\ndef transform(row):\n    return row\n", "transform", "transform"
        )


def test_state_does_not_leak_between_calls():
    handle = _compiled(
        "def transform(row):\n    acc = []\n    acc.append(row['n'])\n    return {'acc': acc}\n"
    )
    assert handle({"n": 1}) == {"acc": [1]}
    assert handle({"n": 2}) == {"acc": [2]}


def test_the_module_is_compiled_once_and_reused(monkeypatch):
    original_parse = starlark.parse
    parse_calls = []

    def counting_parse(*args, **kwargs):
        parse_calls.append(args)
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(starlark, "parse", counting_parse)
    handle = _compiled(DOUBLE)
    parse_calls_after_compile = len(parse_calls)
    assert [handle({"n": i})["n"] for i in range(50)] == [i * 2 for i in range(50)]
    # 50 calls to the compiled function must not trigger any more parsing —
    # a re-parse-every-call implementation would pass the value-correctness
    # assertion above just as well, which is why this counts invocations.
    assert len(parse_calls) == parse_calls_after_compile


def test_the_error_rendering_the_refusal_contract_depends_on_is_unchanged():
    # Pins the exact starlark-pyo3 rendering the sentinel match relies on. If this
    # fails the binding changed its error format — re-derive the matcher in
    # _find_refusal_message, do not loosen this test.
    module = starlark.Module()
    module.add_callable("boom", _raise_step_refused)
    starlark.eval(
        module,
        starlark.parse("probe", "def go():\n    boom('why')\n"),
        starlark.Globals.standard(),
    )
    with pytest.raises(starlark.StarlarkError) as err:
        module.freeze().call("go")
    assert "\nerror: StepRefused: why" in str(err.value)


def _raise_step_refused(reason: str) -> None:
    raise StepRefused(reason)
