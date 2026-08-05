"""The node types as prompt copy, assembled from each stage module's own entry.

Kept out of `app/models/stages/__init__.py`, which imports nothing: importing one
stage module does not pull in the other nine.
"""
from __future__ import annotations

from app.models.stages.aggregate import NODE_TYPE_SPECS as _AGGREGATE
from app.models.stages.code import NODE_TYPE_SPECS as _CODE
from app.models.stages.filter_rows import NODE_TYPE_SPECS as _FILTER_ROWS
from app.models.stages.human_review_queue import NODE_TYPE_SPECS as _HUMAN_REVIEW_QUEUE
from app.models.stages.input_data import NODE_TYPE_SPECS as _INPUT_DATA
from app.models.stages.join import NODE_TYPE_SPECS as _JOIN
from app.models.stages.llm_transform import NODE_TYPE_SPECS as _LLM_TRANSFORM
from app.models.stages.node_spec import NodeTypeSpec
from app.models.stages.publish import NODE_TYPE_SPECS as _PUBLISH
from app.models.stages.starlark import NODE_TYPE_SPECS as _STARLARK
from app.models.stages.union import NODE_TYPE_SPECS as _UNION

# app.agents.compiler.prompt and app.mcp.server render this into their system
# prompts. Merge order fixes the order the prompts list the types in.
NODE_TYPES: dict[str, NodeTypeSpec] = {
    **_INPUT_DATA,
    **_LLM_TRANSFORM,
    **_CODE,
    **_JOIN,
    **_AGGREGATE,
    **_HUMAN_REVIEW_QUEUE,
    **_PUBLISH,
    **_UNION,
    **_FILTER_ROWS,
    **_STARLARK,
}

# The types whose config carries authored code all owe a plain-language `summary`
# and `corner_cases`, refused on write by app.services.stage_edit; their specs carry
# the contract notes, pinned by tests/test_node_type_notes.py.
CODE_CARRYING_TYPES = ("python_row_function", "python_frame_function", "publish",
                       "filter_rows", "starlark_row_function")
