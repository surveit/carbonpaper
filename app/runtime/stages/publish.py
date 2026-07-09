"""Handler for the publish stage type."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.models import Stage
from app.runtime.context import RunContext

from .python_functions import _load_python_function


def handle_publish(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    """Publish stages have a function: block. Run the function and capture its
    output dataframe (paths to artifacts)."""
    publish_cfg = stage.publish
    assert publish_cfg is not None  # Stage validation: publish carries publish_cfg
    # Pass inputs positionally + an output_dir kwarg
    output_dir = publish_cfg.destination or "build/"
    output_dir = str(ctx["run_dir"] / "artifacts" / Path(output_dir).name)

    fn = _load_python_function(stage)
    args = [inputs[ref.id] for ref in stage.inputs]
    result = fn(*args, output_dir=output_dir)
    if not isinstance(result, pd.DataFrame):
        raise ValueError(
            f"publish stage {stage.id}: function must return a DataFrame, "
            f"got {type(result).__name__}"
        )
    return result
