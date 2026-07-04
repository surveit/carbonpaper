"""Handler for the publish stage type."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pandas as pd


def handle_publish(stage: dict[str, Any], inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    """Publish stages have a function: block. Run the function and capture its
    output dataframe (paths to artifacts)."""
    fn_spec = stage.get("function")
    if fn_spec is None:
        raise ValueError(f"publish stage {stage['id']} requires a function: block")
    # Pass inputs positionally + an output_dir kwarg
    publish_cfg = stage.get("publish", {})
    output_dir = publish_cfg.get("destination", "build/")
    output_dir = str(ctx["run_dir"] / "artifacts" / Path(output_dir).name)

    module_name = fn_spec["module"]
    fn_name = fn_spec.get("function", "transform")
    module = importlib.import_module(module_name)
    fn = getattr(module, fn_name)

    args = [inputs[inp["id"]] for inp in stage.get("inputs", [])]
    return fn(*args, output_dir=output_dir)
