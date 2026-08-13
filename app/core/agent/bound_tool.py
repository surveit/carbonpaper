"""A ToolSpec plus what an in-process SDK-MCP server needs to CALL it."""
from __future__ import annotations

import inspect
import json
import typing
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Mapping

from claude_agent_sdk import SdkMcpTool, tool
from pydantic import BaseModel, ConfigDict, Field, create_model

from app.core.agent.tool_spec import ToolSpec

_VARIADIC = (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)


@dataclass(frozen=True)
class BoundToolSpec(ToolSpec):
    fn: Callable[..., Any]
    label: str
    json_schema: dict[str, Any]
    parse_arguments: Callable[[dict[str, Any]], dict[str, Any]]

    def as_sdk_tool(self) -> SdkMcpTool[Any]:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                result = self.fn(**self.parse_arguments(args))
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

        return tool(self.name, self.description, self.json_schema)(handler)


def bind_function(
    *, name: str, description: str, fn: Callable[..., Any], label: str,
    parameters: Mapping[str, str], skip: frozenset[str] = frozenset(),
) -> BoundToolSpec:
    """`parameters` says what each argument IS; what each argument TAKES is the signature."""
    arguments = build_arguments_model(fn, parameters, skip)
    return BoundToolSpec(
        name=name,
        description=description,
        fn=fn,
        label=label,
        json_schema=arguments.model_json_schema(),
        parse_arguments=lambda args: read_arguments(arguments, args),
    )


def bind_to_json_schema(
    *, name: str, description: str, fn: Callable[..., Any], label: str,
    json_schema: dict[str, Any],
) -> BoundToolSpec:
    """For a tool whose arguments are a model the CALLER holds, not a signature to read."""
    return BoundToolSpec(
        name=name,
        description=description,
        fn=fn,
        label=label,
        json_schema=json_schema,
        parse_arguments=lambda args: args,
    )


def build_arguments_model(
    fn: Callable[..., Any], parameters: Mapping[str, str], skip: frozenset[str],
) -> type[BaseModel]:
    signature = inspect.signature(fn)
    unknown = set(parameters) - set(signature.parameters)
    if unknown:
        raise ValueError(f"{fn.__name__} does not take {sorted(unknown)}")
    hints = typing.get_type_hints(fn)
    fields: dict[str, Any] = {}
    for parameter_name, parameter in signature.parameters.items():
        if parameter_name in skip:
            continue
        if parameter.kind in _VARIADIC:
            raise ValueError(f"{fn.__name__} takes *args/**kwargs, so it has no argument model")
        declared = hints.get(parameter_name, Any)
        prose = parameters.get(parameter_name)
        fields[parameter_name] = (
            Annotated[declared, Field(description=prose)] if prose else declared,
            ... if parameter.default is inspect.Parameter.empty else parameter.default,
        )
    return create_model(
        f"{fn.__name__}_arguments",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def read_arguments(arguments: type[BaseModel], args: dict[str, Any]) -> dict[str, Any]:
    """One level deep: a nested model stays a model, which is what the function declared."""
    parsed = arguments.model_validate(args)
    return {name: getattr(parsed, name) for name in args}


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
