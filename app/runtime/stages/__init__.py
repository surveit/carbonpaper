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

from ..options import DEFAULT_PARALLEL
from .aggregate import handle_aggregate
from .execution import (
    FrameTransformHandler,
    Row as Row,
    RowMapTransformHandler,
    SourceHandler,
    StageHandler,
    validate_registry_matches_model,
)
from .filter_rows import build_filter_mapper
from .human_review_queue import build_human_review_mapper
from .input_data import preflight_input_data, read_input_data
from .join import handle_enrich, handle_expand
from .llm_transform import LLMTransformHandler
from .report import handle_report
from .reshape import handle_dedupe, handle_explode, handle_sort_rank
from .python_functions import handle_python_frame_function, build_python_row_mapper
from .starlark_filter import make_starlark_filter_mapper
from .starlark_functions import build_starlark_row_mapper
from .union import handle_union

Preflight = Callable[[WorkflowStage], tuple[list[str], dict[str, Any] | None]]

PREFLIGHTS: dict[StageType, Preflight] = {
    StageType.input_data: preflight_input_data,
}

HANDLERS: dict[StageType, StageHandler] = {
    StageType.input_data: SourceHandler(read_input_data),
    # parallelism stays 1: the mapped function is user-authored code, not assumed thread-safe.
    StageType.python_row_function: RowMapTransformHandler(build_python_row_mapper),
    # No frame handler consults the cache; each model's CACHE_IGNORED_BECAUSE says why.
    StageType.python_frame_function: FrameTransformHandler(handle_python_frame_function),
    StageType.enrich: FrameTransformHandler(handle_enrich),
    StageType.expand: FrameTransformHandler(handle_expand),
    StageType.aggregate: FrameTransformHandler(handle_aggregate),
    StageType.llm_transform: LLMTransformHandler(parallelism=DEFAULT_PARALLEL),
    StageType.human_review_queue: RowMapTransformHandler(
        build_human_review_mapper,
        trims_output_to_declared=True,
    ),
    StageType.report: FrameTransformHandler(handle_report),
    StageType.union: FrameTransformHandler(handle_union),
    # Row-mapped with drops_rows: the runtime drives the predicate row by row
    # and does the selecting itself, so it holds the input ordinals that
    # survived — this stage's lineage — without the handler reporting them.
    StageType.filter_rows: RowMapTransformHandler(build_filter_mapper, drops_rows=True),
    # parallelism stays 1: matching python_row_function's calling convention. The
    # interpreter handle is this execution's, and Starlark freezes module globals,
    # so nothing crosses rows either way.
    StageType.starlark_row_function: RowMapTransformHandler(build_starlark_row_mapper),
    # drops_rows: same contract as filter_rows — the driver does the selecting,
    # so it holds the surviving input ordinals without the predicate reporting them.
    StageType.starlark_filter_rows: RowMapTransformHandler(
        make_starlark_filter_mapper, drops_rows=True),
    StageType.explode: FrameTransformHandler(handle_explode),
    StageType.dedupe: FrameTransformHandler(handle_dedupe),
    StageType.sort_rank: FrameTransformHandler(handle_sort_rank),
}

# A mis-shaped registration (e.g. a frame handler for a type the model declares
# preserving) must not start the app — fail here, at import.
validate_registry_matches_model(HANDLERS)
