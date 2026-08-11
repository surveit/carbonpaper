"""A ToolSpec plus what an in-process SDK-MCP server needs to CALL it."""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable

from claude_agent_sdk import SdkMcpTool, tool
from pydantic import BaseModel

from app.core.agent.tool_spec import ToolSpec


@dataclass(frozen=True)
class BoundToolSpec(ToolSpec):
    fn: Callable[..., Any]
    input_schema: dict[str, object]
    label: str

    def as_sdk_tool(self) -> SdkMcpTool[Any]:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                result = self.fn(**args)
                # An async tool is one that WAITS (get_run_status holding open on a
                # running run); awaiting it here is what keeps the app answering
                # everything else meanwhile.
                if inspect.isawaitable(result):
                    result = await result
                return as_tool_content(result)
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
        # nulls, matching app.models.stage_to_spec_dict. Additive for every other
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
