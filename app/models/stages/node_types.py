"""The node types as prompt copy, assembled from each stage module's own entry.

Assembled here rather than in `app/models/stages/__init__.py`: `app.models.stage_base`
imports `app.models.stages.shared`, so the package `__init__` runs before `StageBase`
exists and cannot import modules that subclass it.
"""
from __future__ import annotations

from typing import Any

from app.models.node_contract_notes import CODE_SUMMARY_CONTRACT_NOTE
from app.models.stages.aggregate import NODE_TYPE_SPECS as _AGGREGATE
from app.models.stages.code import NODE_TYPE_SPECS as _CODE
from app.models.stages.filter_rows import NODE_TYPE_SPECS as _FILTER_ROWS
from app.models.stages.human_review_queue import NODE_TYPE_SPECS as _HUMAN_REVIEW_QUEUE
from app.models.stages.input_data import NODE_TYPE_SPECS as _INPUT_DATA
from app.models.stages.join import NODE_TYPE_SPECS as _JOIN
from app.models.stages.llm_transform import NODE_TYPE_SPECS as _LLM_TRANSFORM
from app.models.stages.publish import NODE_TYPE_SPECS as _PUBLISH
from app.models.stages.union import NODE_TYPE_SPECS as _UNION

# app.agents.compiler.prompt and app.mcp.server render this into their system
# prompts: type -> {summary, blocks, required, optional, min_inputs,
# requires_inputs}. `blocks` names the config blocks that type's stage model
# requires. The models do not expose this rendering shape, so the copy is plain
# data. Merge order fixes the order the prompts list the types in.
NODE_TYPES: dict[str, dict[str, Any]] = {
    **_INPUT_DATA,
    **_LLM_TRANSFORM,
    **_CODE,
    **_JOIN,
    **_AGGREGATE,
    **_HUMAN_REVIEW_QUEUE,
    **_PUBLISH,
    **_UNION,
    **_FILTER_ROWS,
}

# The types whose config carries authored code all owe a plain-language `summary`.
# Applied here rather than written into each entry, so a new code-carrying type
# cannot ship having forgotten it.
CODE_CARRYING_TYPES = ("python_row_function", "python_frame_function", "publish", "filter_rows")
for _type_name in CODE_CARRYING_TYPES:
    _spec = NODE_TYPES[_type_name]
    _spec["notes"] = f"{_spec['notes']} {CODE_SUMMARY_CONTRACT_NOTE}"
    _spec["optional"] = [*_spec["optional"], "summary"]
