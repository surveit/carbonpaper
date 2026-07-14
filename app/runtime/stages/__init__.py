"""Stage handlers — one module per stage type (the two python-function grains
share python_functions.py). `HANDLERS` maps stage type to a shaped handler:
the shape (row-mapped / source / frame) fixes what the runtime hands the
handler — see execution.py. `execute_handler` is the single dispatch the
runner and preview go through."""

from __future__ import annotations

from app.models.stage import StageType

from ..options import DEFAULT_PARALLEL
from ._shared import HaltForReview
from .aggregate import handle_aggregate
from .execution import (
    FrameHandler,
    Row,
    RowMapHandler,
    SourceHandler,
    StageHandler,
    check_registry_matches_model,
    execute_handler,
)
from .human_review_queue import handle_human_review_queue
from .input_data import read_input_data
from .join import handle_join
from .llm_transform import make_llm_row_mapper
from .publish import handle_publish
from .python_functions import handle_python_frame_function, make_python_row_mapper

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
check_registry_matches_model(HANDLERS)

__all__ = [
    "HANDLERS",
    "HaltForReview",
    "FrameHandler",
    "Row",
    "RowMapHandler",
    "SourceHandler",
    "StageHandler",
    "check_registry_matches_model",
    "execute_handler",
    "handle_aggregate",
    "handle_human_review_queue",
    "handle_join",
    "handle_publish",
    "handle_python_frame_function",
    "make_llm_row_mapper",
    "make_python_row_mapper",
    "read_input_data",
]
