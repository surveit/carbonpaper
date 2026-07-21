"""Stage handlers — one module per stage type (the two python-function grains
share python_functions.py). `HANDLERS` maps stage type to a shaped handler:
the shape (row-mapped / source / frame) fixes what the runtime hands the
handler — see execution.py. The runner and preview run a stage through
`handler.execute(...)`.

`PREFLIGHTS` maps a stage type to its prepare-time readiness check: given the
stage (with any run bindings already applied), return (issues, record) —
human-readable issues naming what stops this stage from running ([] = ready),
and a provenance record for the run manifest (None when unready). Only types
whose readiness a valid model can't guarantee register one; the runner calls
whatever is registered without knowing what any stage type checks."""

from __future__ import annotations

from typing import Any, Callable

from app.models.stage import Stage, StageType

from ..errors import HaltForReview
from ..options import DEFAULT_PARALLEL
from .aggregate import handle_aggregate
from .execution import (
    FrameHandler,
    Row,
    RowMapHandler,
    SourceHandler,
    StageHandler,
    validate_registry_matches_model,
)
from .human_review_queue import handle_human_review_queue
from .input_data import preflight_input_data, read_input_data
from .join import handle_join
from .llm_transform import make_llm_row_mapper
from .publish import handle_publish
from .python_functions import handle_python_frame_function, make_python_row_mapper

Preflight = Callable[[Stage], tuple[list[str], dict[str, Any] | None]]

PREFLIGHTS: dict[StageType, Preflight] = {
    StageType.input_data: preflight_input_data,
}

HANDLERS: dict[StageType, StageHandler] = {
    StageType.input_data: SourceHandler(read_input_data),
    # parallelism stays 1: the mapped function is user-authored code, not assumed thread-safe.
    StageType.python_row_function: RowMapHandler(make_python_row_mapper),
    StageType.python_frame_function: FrameHandler(handle_python_frame_function),
    StageType.join_: FrameHandler(handle_join),
    StageType.aggregate: FrameHandler(handle_aggregate),
    StageType.llm_transform: RowMapHandler(
        make_llm_row_mapper,
        parallelism=DEFAULT_PARALLEL,
        project_output_to_declared=True,
    ),
    StageType.human_review_queue: FrameHandler(handle_human_review_queue),
    StageType.publish: FrameHandler(handle_publish),
}

# A mis-shaped registration (e.g. a frame handler for a type the model declares
# preserving) must not start the app — fail here, at import.
validate_registry_matches_model(HANDLERS)

__all__ = [
    "HANDLERS",
    "PREFLIGHTS",
    "Preflight",
    "HaltForReview",
    "FrameHandler",
    "Row",
    "RowMapHandler",
    "SourceHandler",
    "StageHandler",
    "validate_registry_matches_model",
    "handle_aggregate",
    "handle_human_review_queue",
    "handle_join",
    "handle_publish",
    "handle_python_frame_function",
    "make_llm_row_mapper",
    "make_python_row_mapper",
    "preflight_input_data",
    "read_input_data",
]
