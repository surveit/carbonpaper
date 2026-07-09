"""Stage handlers — one module per stage type (the two python-function grains
share python_functions.py). `HANDLERS` maps stage type to handler; the runner
dispatches on stage type. Handlers consume typed Stage objects."""

from __future__ import annotations

from typing import Callable

import pandas as pd

from app.models import Stage
from app.runtime.context import RunContext

from ._shared import HaltForReview
from .aggregate import handle_aggregate
from .human_review_queue import handle_human_review_queue
from .input_data import handle_input_data
from .join import handle_join
from .llm_transform import handle_llm_transform
from .publish import handle_publish
from .python_functions import handle_python_frame_function, handle_python_row_function

# A stage handler: given the stage spec, its inputs keyed by upstream id, and the
# run context, produce the stage's output frame (or None for side-effect-only
# stages like publish).
StageHandler = Callable[[Stage, dict[str, pd.DataFrame], RunContext], pd.DataFrame | None]

HANDLERS: dict[str, StageHandler] = {
    "input_data": handle_input_data,
    "python_row_function": handle_python_row_function,
    "python_frame_function": handle_python_frame_function,
    "join": handle_join,
    "aggregate": handle_aggregate,
    "llm_transform": handle_llm_transform,
    "human_review_queue": handle_human_review_queue,
    "publish": handle_publish,
}

__all__ = [
    "HANDLERS",
    "HaltForReview",
    "handle_input_data",
    "handle_python_row_function",
    "handle_python_frame_function",
    "handle_join",
    "handle_aggregate",
    "handle_llm_transform",
    "handle_human_review_queue",
    "handle_publish",
]
