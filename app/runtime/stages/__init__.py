"""Stage handlers — one module per stage type. `HANDLERS` maps stage type to
handler; the runner dispatches on stage type."""

from __future__ import annotations

from ._shared import HaltForReview
from .aggregate import handle_aggregate
from .human_review_queue import handle_human_review_queue
from .input_data import handle_input_data
from .join import handle_join
from .llm_transform import handle_llm_transform
from .publish import handle_publish
from .python_transform import handle_python_transform

HANDLERS = {
    "input_data": handle_input_data,
    "python_transform": handle_python_transform,
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
    "handle_python_transform",
    "handle_join",
    "handle_aggregate",
    "handle_llm_transform",
    "handle_human_review_queue",
    "handle_publish",
]
