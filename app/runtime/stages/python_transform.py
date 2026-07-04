"""Handler for the python_transform stage type."""

from __future__ import annotations

import importlib
from typing import Any

import pandas as pd


def handle_python_transform(stage: dict[str, Any], inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    fn_spec = stage.get("function") or {}
    kind = fn_spec.get("kind")
    if kind == "module":
        module_name = fn_spec["module"]
        fn_name = fn_spec.get("function", "transform")
        module = importlib.import_module(module_name)
        fn = getattr(module, fn_name)
    elif kind == "inline":
        code = fn_spec.get("code", "")
        ns: dict[str, Any] = {}
        exec(code, ns)
        fn_name = fn_spec.get("function", "transform")
        fn = ns.get(fn_name) or ns.get("transform")
        if fn is None:
            raise ValueError(f"Inline function 'transform' not defined for stage {stage['id']}")
    else:
        raise ValueError(f"Unknown function kind for stage {stage['id']}: {kind}")

    # Pass dataframes positionally in declared input order.
    args = [inputs[inp["id"]] for inp in stage.get("inputs", [])]
    return fn(*args)
