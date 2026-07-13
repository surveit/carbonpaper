"""Stage handlers — one module per stage type (the two python-function grains
share python_functions.py). `HANDLERS` maps stage type to a shaped handler:
the shape (row-mapped / source / frame) fixes what the runtime hands the
handler — see execution.py. `execute_handler` is the single dispatch the
runner and preview go through."""

from __future__ import annotations

from app.models.stage import StageType

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
from .llm_transform import handle_llm_transform
from .publish import handle_publish
from .python_functions import handle_python_frame_function, make_python_row_mapper

HANDLERS: dict[StageType, StageHandler] = {
    StageType.input_data: SourceHandler(read_input_data),
    StageType.python_row_function: RowMapHandler(make_python_row_mapper),
    StageType.python_frame_function: FrameHandler(handle_python_frame_function),
    StageType.join_: FrameHandler(handle_join),
    StageType.aggregate: FrameHandler(handle_aggregate),
    StageType.llm_transform: FrameHandler(handle_llm_transform),
    StageType.human_review_queue: FrameHandler(handle_human_review_queue),
    StageType.publish: FrameHandler(handle_publish),
}

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
    "handle_llm_transform",
    "handle_publish",
    "handle_python_frame_function",
    "make_python_row_mapper",
    "read_input_data",
]
