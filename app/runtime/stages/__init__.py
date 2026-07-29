"""Stage handlers - one module per stage type. `HANDLERS` maps a type to a shaped
handler (row-mapped / source / frame); the shape fixes what the runtime hands
the handler. `PREFLIGHTS` maps a type to its prepare-time readiness check,
returning (issues, record). Only types whose readiness a valid model can't
guarantee register one; the runner calls whatever is registered.
"""

from __future__ import annotations

from typing import Any, Callable

from app.models.stage import Stage, StageType

from ..errors import HaltForReview
from ..options import DEFAULT_PARALLEL
from .aggregate import handle_aggregate
from .execution import (
    FrameHandler,
    LLMTransformHandler,
    Row,
    RowMapHandler,
    SourceHandler,
    StageHandler,
    validate_registry_matches_model,
)
from .filter_rows import make_filter_mapper
from .human_review_queue import make_human_review_mapper
from .input_data import preflight_input_data, read_input_data
from .join import handle_join
from .llm_transform import make_llm_row_mapper, run_llm_batches
from .publish import handle_publish
from .python_functions import handle_python_frame_function, make_python_row_mapper
from .union import handle_union

Preflight = Callable[[Stage], tuple[list[str], dict[str, Any] | None]]

PREFLIGHTS: dict[StageType, Preflight] = {
    StageType.input_data: preflight_input_data,
}

HANDLERS: dict[StageType, StageHandler] = {
    StageType.input_data: SourceHandler(read_input_data),
    # parallelism stays 1: the mapped function is user-authored code, not assumed thread-safe.
    StageType.python_row_function: RowMapHandler(make_python_row_mapper),
    StageType.python_frame_function: FrameHandler(handle_python_frame_function),
    # caches_frames=False: join and aggregate are bounded vectorised primitives
    # whose compute is lower-order than the hash of their own input, so
    # fingerprinting the inputs costs more than the pandas operation a hit would
    # skip — the cache would only ever slow them down. python_frame_function
    # above runs arbitrary user code of unbounded cost and does cache.
    StageType.join_: FrameHandler(handle_join, caches_frames=False),
    StageType.aggregate: FrameHandler(handle_aggregate, caches_frames=False),
    StageType.llm_transform: LLMTransformHandler(
        make_llm_row_mapper,
        run_llm_batches,
        parallelism=DEFAULT_PARALLEL,
        project_output_to_declared=True,
    ),
    StageType.human_review_queue: RowMapHandler(
        make_human_review_mapper,
        project_output_to_declared=True,
    ),
    # caches_frames=False: publish is terminal and side-effecting — it writes
    # artifacts the world reads, not an output a later run consumes. Replaying a
    # cached frame would skip the write and leave this run's artifacts absent.
    StageType.publish: FrameHandler(handle_publish, caches_frames=False),
    # caches_frames=False: concatenation is a bounded vectorised primitive,
    # same reasoning as join/aggregate above.
    StageType.union: FrameHandler(handle_union, caches_frames=False),
    # Row-mapped with drops_rows: the runtime drives the predicate row by row
    # and does the selecting itself, so it holds the input ordinals that
    # survived — this stage's lineage — without the handler reporting them.
    # caches_rows=False: deciding a row costs less than fingerprinting it.
    StageType.filter_rows: RowMapHandler(
        make_filter_mapper, drops_rows=True, caches_rows=False
    ),
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
    "LLMTransformHandler",
    "Row",
    "RowMapHandler",
    "SourceHandler",
    "StageHandler",
    "validate_registry_matches_model",
    "handle_aggregate",
    "make_human_review_mapper",
    "handle_join",
    "handle_publish",
    "handle_python_frame_function",
    "make_llm_row_mapper",
    "run_llm_batches",
    "make_python_row_mapper",
    "preflight_input_data",
    "read_input_data",
    "handle_union",
    "make_filter_mapper",
]
