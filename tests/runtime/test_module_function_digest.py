"""Resolving a kind=module function re-checks the pinned digest at stage start:
the module lives outside the stage spec (and outside a frozen version), so drift
must be detected, not assumed away."""
from __future__ import annotations

import importlib
import sys

import pytest

from app.models import Stage
from app.runtime.stages.python_functions import _load_python_function

_SCHEMA = {"columns": [{"name": "a", "type": "str", "nullable": False}]}
_PASSTHROUGH = "def transform(row):\n    return row\n"
_CHANGED = "def transform(row):\n    return {**row, 'a': 'x'}\n"


@pytest.fixture
def module_on_path(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    name = "carbonpaper_runtime_digest_fixture"
    path = tmp_path / f"{name}.py"

    def write(source: str) -> None:
        path.write_text(source, encoding="utf-8")
        importlib.invalidate_caches()
        sys.modules.pop(name, None)

    write(_PASSTHROUGH)
    yield name, write
    sys.modules.pop(name, None)


def _module_stage(name: str) -> Stage:
    return Stage.model_validate({
        "id": "step",
        "type": "python_row_function",
        "name": "Step",
        "inputs": [{"id": "src", "schema": _SCHEMA}],
        "output_schema": _SCHEMA,
        "function": {"kind": "module", "module": name},
    })


def test_load_python_function_returns_the_module_callable(module_on_path):
    name, _ = module_on_path
    fn = _load_python_function(_module_stage(name))
    assert fn({"a": "1"}) == {"a": "1"}


def test_load_python_function_refuses_a_module_that_changed_since_the_stage_was_written(
    module_on_path,
):
    name, write = module_on_path
    stage = _module_stage(name)
    write(_CHANGED)
    with pytest.raises(ValueError, match="has changed since this stage definition"):
        _load_python_function(stage)


def test_load_python_function_refuses_a_module_that_disappeared(module_on_path, tmp_path):
    name, _ = module_on_path
    stage = _module_stage(name)
    (tmp_path / f"{name}.py").unlink()
    sys.modules.pop(name, None)
    importlib.invalidate_caches()
    with pytest.raises(ValueError, match="cannot be resolved"):
        _load_python_function(stage)
