"""The write set, enforced on every row: a column the signature does not write
flows through untouched, and a mapper that changes one fails its stage. Output
validation cannot see this — a flowing column is in the promised schema carrying
its ORIGINAL type, so a wrong value of the right type passes clean."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from app.models import parse_stage
from app.models.stage import StageType
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt
from conftest import make_run_context, reads_of

# One row of the LDA export's shape: a name, the money column, and a date. The
# money column is what a mapper is caught clobbering below.
_COLUMNS = [
    {"name": "client", "type": "str", "nullable": False},
    {"name": "amount_usd", "type": "float", "nullable": False},
]
_SRC = pd.DataFrame({"client": ["Akin Gump"], "amount_usd": [40000.0]})


def _python_stage(code: str, rewrites: list[dict] | None = None):
    return parse_stage({
        "id": "score", "description": "Score the filing", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": {"columns": _COLUMNS}}],
        "signature": {
            "form": "extends",
            "reads": reads_of("load", _COLUMNS),
            "adds": [{"name": "note", "type": "str", "nullable": True}],
            "rewrites": rewrites or [],
        },
        "function": {"kind": "inline", "code": code},
    })


def _run_python(code: str, rewrites: list[dict] | None = None, src: pd.DataFrame = _SRC):
    return HANDLERS[StageType.python_row_function].execute(
        _python_stage(code, rewrites), {"load": src.copy()}, make_run_context()
    )


def test_a_mapper_may_not_change_a_column_it_only_reads():
    code = "def transform(row):\n    return {'note': 'x', 'amount_usd': 0.0}\n"
    with pytest.raises(ValueError, match=r"amount_usd"):
        _run_python(code)


def test_the_same_change_is_allowed_once_the_signature_rewrites_it():
    code = "def transform(row):\n    return {'note': 'x', 'amount_usd': 0.0}\n"
    out = _run_python(code, rewrites=[{"name": "amount_usd", "type": "float", "nullable": False}])
    assert list(out["amount_usd"]) == [0.0]


def test_carrying_the_whole_row_through_unchanged_is_not_a_write():
    """The documented idiom (`return {**row, ...}`) returns every read; none of them changed."""
    out = _run_python("def transform(row):\n    return {**row, 'note': 'x'}\n")
    assert list(out["amount_usd"]) == [40000.0]
    assert list(out["note"]) == ["x"]


def test_a_column_the_mapper_never_read_is_still_held_to_flowing():
    """The rejoin lets an invented key win over the input, so reading it is not what makes it a write."""
    stage = parse_stage({
        "id": "score", "description": "Score the filing", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": {"columns": _COLUMNS}}],
        "signature": {
            "form": "extends",
            "reads": reads_of("load", [_COLUMNS[0]]),
            "adds": [{"name": "note", "type": "str", "nullable": True}],
        },
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    return {'note': 'x', 'amount_usd': 0.0}\n"},
    })
    with pytest.raises(ValueError, match=r"amount_usd"):
        HANDLERS[StageType.python_row_function].execute(
            stage, {"load": _SRC.copy()}, make_run_context())


def test_a_representation_change_is_not_a_value_change():
    """Starlark holds no dates, so a carried-through date comes back as its ISO text."""
    columns = [*_COLUMNS, {"name": "filed_on", "type": "date", "nullable": False}]
    stage = parse_stage({
        "id": "score", "description": "Score the filing", "type": "starlark_row_function",
        "inputs": [{"id": "load", "schema": {"columns": columns}}],
        "signature": {
            "form": "extends",
            "reads": reads_of("load", columns),
            "adds": [{"name": "note", "type": "str", "nullable": True}],
        },
        "starlark": {"code": "def transform(row):\n    return dict(row, note = 'x')\n"},
    })
    src = _SRC.assign(filed_on=[dt.date(2024, 1, 17)])
    out = HANDLERS[StageType.starlark_row_function].execute(
        stage, {"load": src}, make_run_context())
    assert list(out["note"]) == ["x"]
    assert list(out["amount_usd"]) == [40000.0]


# ── the batched llm_transform path, which assembles its own frame ────────────
# The reply spec compiled from `adds` is what a real model is held to, so an
# extra key does not survive `call_llm_batch`. What is checked here is the SHAPE's
# side of the `RunBatches` contract, on the same footing as the row-count check in
# `_order_by_input_position`: the driver does not take the batch handler's word.


def _batched_stage():
    return parse_stage({
        "id": "score", "description": "Score the filing", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": {"columns": _COLUMNS}}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": [_COLUMNS[0]]}],
            "adds": [{"name": "note", "type": "str", "nullable": True}],
        },
        "llm": {"prompt_data_template": "score {client}", "batch_size": 2, "max_retries": 0},
    })


def _run_batched(monkeypatch, reply: dict):
    monkeypatch.setattr(lt, "call_llm_batch", lambda *a, **k: reply)
    return HANDLERS[StageType.llm_transform].execute(
        _batched_stage(), {"load": _SRC.copy()}, make_run_context())


def test_a_batched_reply_may_not_change_a_flowing_column(monkeypatch):
    reply = {"results": [{"row_number": 0, "note": "x", "amount_usd": 0.0}]}
    with pytest.raises(ValueError, match=r"amount_usd"):
        _run_batched(monkeypatch, reply)


def test_a_batched_reply_that_only_adds_passes(monkeypatch):
    out = _run_batched(monkeypatch, {"results": [{"row_number": 0, "note": "x"}]})
    assert list(out["note"]) == ["x"]
    assert list(out["amount_usd"]) == [40000.0]
