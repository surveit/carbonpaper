"""One tool as the model meets it, so a description is a value a reviewer can see change.

`ToolSpec` is what any runtime needs to describe a tool; `BoundToolSpec` adds what an
in-process SDK-MCP server needs to CALL one. A runtime that derives the schema from the
signature (FastMCP) takes the former.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from claude_agent_sdk import SdkMcpTool, tool
from pydantic import BaseModel


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str


@dataclass(frozen=True)
class BoundToolSpec(ToolSpec):
    fn: Callable[..., Any]
    input_schema: dict[str, object]
    label: str

    def as_sdk_tool(self) -> SdkMcpTool[Any]:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return as_tool_content(self.fn(**args))
            except Exception as exc:  # noqa: BLE001 — tool boundary: any tool failure is surfaced to the model as an error, never swallowed or faked
                return {
                    "content": [{"type": "text", "text": f"ERROR: {exc}"}],
                    "is_error": True,
                }

        return tool(self.name, self.description, self.input_schema)(handler)


def as_tool_content(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        text = value
    else:
        # by_alias + exclude_none so a model carrying Stage(s) (e.g. a draft view)
        # comes back to the agent in the SAME spec-dict form it writes stages
        # in — aliased (`schema`, not `table_schema`) and without the unset-optional
        # nulls, matching loader.stage_to_spec_dict. Additive for every other
        # model-returning tool: an alias-free model (DraftView, DraftEdit,
        # SaveResult, ...) dumps equivalently (a dropped null re-parses as its
        # default).
        dumpable = (
            value.model_dump(mode="json", by_alias=True, exclude_none=True)
            if isinstance(value, BaseModel)
            else value
        )
        text = json.dumps(dumpable, default=str, indent=2)
    return {"content": [{"type": "text", "text": text}]}
