"""Handler for the publish stage type."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.models import Stage

from ..context import RunContext
from .python_functions import _load_python_function


def handle_publish(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    """Publish stages have a function: block. Run the function and capture its
    output dataframe (paths to artifacts)."""
    publish_cfg = stage.publish
    assert publish_cfg is not None  # Stage validation: publish carries publish_cfg
    # The runtime owns the run-dir layout, so it guarantees output_dir exists
    # before the authored function runs — the function just writes into it.
    output_dir = ctx.require_run_dir() / "artifacts" / Path(publish_cfg.destination or "build/").name
    output_dir.mkdir(parents=True, exist_ok=True)

    fn = _load_python_function(stage)
    # Pass inputs positionally + an output_dir kwarg
    args = [inputs[ref.id] for ref in stage.inputs]
    return fn(*args, output_dir=str(output_dir))
