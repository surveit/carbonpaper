"""Stage handlers - one module per stage type. `HANDLERS` maps a type to a shaped
handler (row-mapped / source / frame); the shape fixes what the runtime hands
the handler. `PREFLIGHTS` maps a type to its prepare-time readiness check,
returning (issues, record). Only types whose readiness a valid model can't
guarantee register one; the runner calls whatever is registered.
"""

from __future__ import annotations

from typing import Any, Callable

from app.models.stage import StageType
from app.models.workflow_stage import WorkflowStage

from ..errors import HaltForReview as HaltForReview
from ..options import DEFAULT_PARALLEL
from .aggregate import handle_aggregate
from .execution import (
    FrameTransformHandler,
    LLMTransformHandler,
    Row as Row,
    RowMapTransformHandler,
    SourceHandler,
    StageHandler,
    validate_registry_matches_model,
)
from .filter_rows import make_filter_mapper
from .human_review_queue import make_human_review_mapper
from .input_data import preflight_input_data, read_input_data
from .join import handle_enrich, handle_expand
from .llm_transform import make_llm_row_mapper, run_llm_batches
from .publish import handle_publish
from .python_functions import handle_python_frame_function, make_python_row_mapper
from .starlark_functions import make_starlark_row_mapper
from .union import handle_union

Preflight = Callable[[WorkflowStage], tuple[list[str], dict[str, Any] | None]]

PREFLIGHTS: dict[StageType, Preflight] = {
    StageType.input_data: preflight_input_data,
}

HANDLERS: dict[StageType, StageHandler] = {
    StageType.input_data: SourceHandler(read_input_data),
    # parallelism stays 1: the mapped function is user-authored code, not assumed thread-safe.
    StageType.python_row_function: RowMapTransformHandler(make_python_row_mapper),
    StageType.python_frame_function: FrameTransformHandler(handle_python_frame_function),
    # caches_frames=False REFUSES caching whatever the stage declares, which is a
    # stronger statement than `Stage.cache`'s per-type default: the joins
    # (enrich/expand) and aggregate are bounded vectorised primitives whose
    # compute is lower-order than the hash of their own input, so fingerprinting
    # the inputs costs more than the pandas operation a hit would skip — there is
    # no workflow in which an author turning it on would be right.
    StageType.enrich: FrameTransformHandler(handle_enrich, caches_frames=False),
    StageType.expand: FrameTransformHandler(handle_expand, caches_frames=False),
    StageType.aggregate: FrameTransformHandler(handle_aggregate, caches_frames=False),
    StageType.llm_transform: LLMTransformHandler(
        make_llm_row_mapper,
        run_llm_batches,
        parallelism=DEFAULT_PARALLEL,
        trims_output_to_declared=True,
    ),
    StageType.human_review_queue: RowMapTransformHandler(
        make_human_review_mapper,
        trims_output_to_declared=True,
    ),
    # caches_frames=False: publish is terminal and side-effecting — it writes
    # artifacts the world reads, not an output a later run consumes. Replaying a
    # cached frame would skip the write and leave this run's artifacts absent.
    StageType.publish: FrameTransformHandler(handle_publish, caches_frames=False),
    # caches_frames=False: concatenation is a bounded vectorised primitive,
    # same reasoning as the joins/aggregate above.
    StageType.union: FrameTransformHandler(handle_union, caches_frames=False),
    # Row-mapped with drops_rows: the runtime drives the predicate row by row
    # and does the selecting itself, so it holds the input ordinals that
    # survived — this stage's lineage — without the handler reporting them.
    StageType.filter_rows: RowMapTransformHandler(make_filter_mapper, drops_rows=True),
    # parallelism stays 1: matching python_row_function's calling convention. The
    # interpreter handle is this execution's, and Starlark freezes module globals,
    # so nothing crosses rows either way.
    StageType.starlark_row_function: RowMapTransformHandler(make_starlark_row_mapper),
}

# A mis-shaped registration (e.g. a frame handler for a type the model declares
# preserving) must not start the app — fail here, at import.
validate_registry_matches_model(HANDLERS)
