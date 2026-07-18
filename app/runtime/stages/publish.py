"""Handler for the publish stage type."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from app.core.models import Stage

from .python_functions import _load_python_function


def handle_publish(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    """Publish stages carry a `function:` block AND a `publish:` block. The
    function is a whole-frame, terminal operation: it sees the full input
    frame(s) plus an `output_dir`, WRITES artifacts there, and returns a manifest
    frame — one row per written artifact. This is why publish is a FrameHandler,
    not a row map (issue #125): rendering one combined report / cross-linked set
    needs many rows at once.

    The returned frame must carry the column `publish.path_column` (default
    `path`) holding each artifact's path — that is the declared answer to "which
    output column is the path". We check it here so a function that forgets it
    fails with a clear message instead of leaving downstream/UI code to guess."""
    publish_cfg = stage.publish
    assert publish_cfg is not None  # Stage validation: publish carries publish_cfg
    # Pass inputs positionally + an output_dir kwarg
    output_dir = publish_cfg.destination or "build/"
    output_dir = str(ctx["run_dir"] / "artifacts" / Path(output_dir).name)

    fn = _load_python_function(stage)
    args = [inputs[ref.id] for ref in stage.inputs]
    result = fn(*args, output_dir=output_dir)

    path_column = publish_cfg.path_column
    if isinstance(result, pd.DataFrame) and not result.empty and path_column not in result.columns:
        raise ValueError(
            f"publish stage {stage.id}: function returned a frame without the declared "
            f"path column {path_column!r} (columns: {list(result.columns)}). Either return "
            f"that column or set publish.path_column to the column that holds artifact paths."
        )
    return result
