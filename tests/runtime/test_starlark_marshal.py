import datetime as dt

import numpy as np
import pandas as pd
import pytest

from app.runtime.stages.starlark_marshal import MAX_EXACT_INT, marshal_row_for_starlark


def test_passes_json_native_values_through_unchanged():
    row = {"name": "acme", "n": 3, "ratio": 0.5, "ok": True, "missing": None}
    assert marshal_row_for_starlark(row) == row


def test_converts_numpy_scalars_to_python_scalars():
    out = marshal_row_for_starlark({"i": np.int64(42), "f": np.float64(3.5), "b": np.bool_(True)})
    assert out == {"i": 42, "f": 3.5, "b": True}
    assert [type(v) for v in out.values()] == [int, float, bool]


@pytest.mark.parametrize("missing", [float("nan"), np.float64("nan"), pd.NaT, pd.NA, None])
def test_every_missing_marker_becomes_none(missing):
    assert marshal_row_for_starlark({"x": missing}) == {"x": None}


def test_datetimes_become_iso_strings():
    row = {"ts": dt.datetime(2026, 8, 3, 14, 30), "d": dt.date(2026, 8, 3)}
    assert marshal_row_for_starlark(row) == {"ts": "2026-08-03T14:30:00", "d": "2026-08-03"}


def test_nested_dicts_and_lists_are_converted_elementwise():
    row = {"meta": {"n": np.int64(7)}, "tags": [np.int64(1), "x"]}
    assert marshal_row_for_starlark(row) == {"meta": {"n": 7}, "tags": [1, "x"]}


def test_oversized_int_raises_naming_the_column_rather_than_losing_precision():
    with pytest.raises(ValueError) as err:
        marshal_row_for_starlark({"filing_id": 2**70 + 7})
    assert "filing_id" in str(err.value)
    assert str(2**70 + 7) in str(err.value)


def test_the_boundary_value_itself_is_allowed():
    assert marshal_row_for_starlark({"n": MAX_EXACT_INT}) == {"n": MAX_EXACT_INT}


def test_oversized_int_message_states_the_guarantee_not_a_false_mechanism():
    # Regression: the message once claimed crossing the boundary "would convert
    # it to a float, silently losing the exact value" — false right at the
    # boundary, since 2**63 round-trips through float exactly (it's a power of
    # two). State the guaranteed-exact bound, not an always-lossy mechanism.
    with pytest.raises(ValueError) as err:
        marshal_row_for_starlark({"n": 2**63})
    assert "convert it to a float" not in str(err.value)
    assert "guaranteed" in str(err.value)


def test_unrepresentable_type_raises_rather_than_being_stringified():
    with pytest.raises(ValueError) as err:
        marshal_row_for_starlark({"blob": object()})
    assert "blob" in str(err.value)


def test_bools_are_not_marshalled_as_ints():
    # bool subclasses int; the int branch must not swallow it.
    out = marshal_row_for_starlark({"flag": True})
    assert out["flag"] is True and type(out["flag"]) is bool


def test_the_binding_really_does_mangle_ints_above_the_guard():
    # The guard's justification, pinned. If this goes green the binding changed:
    # re-derive the boundary from observation, do not delete the guard.
    import starlark

    module = starlark.Module()
    starlark.eval(
        module,
        starlark.parse("probe", "def echo(x):\n    return x\n"),
        starlark.Globals.standard(),
    )
    assert module.freeze().call("echo", {"n": 2**70 + 7})["n"] != 2**70 + 7
