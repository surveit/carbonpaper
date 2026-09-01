"""The stage types as prompt copy, assembled from each stage module's own entry.

Kept out of `app/models/stages/__init__.py`, which imports nothing: importing one
stage module does not pull in the other nine.
"""
from __future__ import annotations

from app.models.stages.aggregate import STAGE_TYPE_SPECS as _AGGREGATE
from app.models.stages.code import STAGE_TYPE_SPECS as _CODE
from app.models.stages.dedupe import STAGE_TYPE_SPECS as _DEDUPE
from app.models.stages.explode import STAGE_TYPE_SPECS as _EXPLODE
from app.models.stages.filter_rows import STAGE_TYPE_SPECS as _FILTER_ROWS
from app.models.stages.human_review_queue import STAGE_TYPE_SPECS as _HUMAN_REVIEW_QUEUE
from app.models.stages.input_data import STAGE_TYPE_SPECS as _INPUT_DATA
from app.models.stages.join import STAGE_TYPE_SPECS as _JOIN
from app.models.stages.llm_transform import STAGE_TYPE_SPECS as _LLM_TRANSFORM
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.report import STAGE_TYPE_SPECS as _REPORT
from app.models.stages.sort_rank import STAGE_TYPE_SPECS as _SORT_RANK
from app.models.stages.starlark import STAGE_TYPE_SPECS as _STARLARK
from app.models.stages.starlark_filter import STAGE_TYPE_SPECS as _STARLARK_FILTER
from app.models.stages.starlark_report import STAGE_TYPE_SPECS as _STARLARK_REPORT
from app.models.stages.union import STAGE_TYPE_SPECS as _UNION

# app.agents.compiler.prompt and app.mcp.server render this into their system
# prompts. Merge order fixes the order the prompts list the types in.
STAGE_TYPES: dict[str, StageTypeSpec] = {
    **_INPUT_DATA,
    **_LLM_TRANSFORM,
    **_CODE,
    **_JOIN,
    **_AGGREGATE,
    **_HUMAN_REVIEW_QUEUE,
    **_REPORT,
    **_UNION,
    **_FILTER_ROWS,
    **_STARLARK,
    **_STARLARK_FILTER,
    **_STARLARK_REPORT,
    **_EXPLODE,
    **_DEDUPE,
    **_SORT_RANK,
}

# Types a project may use only after its owner has approved unsandboxed code
# execution (app.services.code_approval). Withheld from the catalog every prompt
# renders, because that catalog is built once per SERVER and cannot vary by
# project — the refusal happens on WRITE instead, in app.services.stage_edit,
# where the project is known and the message can say how to turn it on. The
# catalog still SAYS they exist (CODE_EXECUTION_ESCAPE_NOTE), or a model with a
# step it cannot express would conclude the step is impossible rather than ask.
# A stored stage of either type keeps loading and running.
APPROVAL_REQUIRED_TYPES = (
    "python_row_function", "python_frame_function", "filter_rows",
)

# What the authoring prompts list. STAGE_TYPES stays whole: the runtime, the
# diagrams and the trace all read it for types a stored workflow may still carry.
AUTHORABLE_TYPES: dict[str, StageTypeSpec] = {
    name: spec for name, spec in STAGE_TYPES.items()
    if name not in APPROVAL_REQUIRED_TYPES
}

# The types whose config carries authored code all owe a plain-language `summary`
# and `corner_cases`, refused on write by app.services.stage_edit; their specs carry
# the contract notes, pinned by tests/test_stage_type_notes.py.
CODE_CARRYING_TYPES = ("python_row_function", "python_frame_function", "report",
                       "filter_rows", "starlark_row_function", "starlark_filter_rows",
                       "starlark_report")

# The subset a prompt states the code-description rule for: a withheld type's rule
# is still enforced on a stored stage, but stating it beside the offered types
# would read as an invitation to author one.
AUTHORABLE_CODE_CARRYING_TYPES = tuple(
    name for name in CODE_CARRYING_TYPES if name in AUTHORABLE_TYPES
)
