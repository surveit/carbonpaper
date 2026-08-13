"""One callable tool, described in terms no LLM provider owns: JSON Schema and pydantic.
Which provider is handed it, and how, is app.core.agent.registry's business.
"""
from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, create_model

_VARIADIC = (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)


@dataclass(frozen=True)
class BoundToolSpec:
    name: str
    description: str
    fn: Callable[..., Any]
    label: str
    json_schema: dict[str, Any]
    parse_arguments: Callable[[dict[str, Any]], dict[str, Any]]


def bind_by_signature(
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


def bind_by_schema(
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
    undescribed = (set(signature.parameters) - skip) - set(parameters)
    if undescribed:
        raise ValueError(
            f"{fn.__name__} advertises {sorted(undescribed)} to the model with no prose "
            "saying what they are — describe them, or add them to `skip`"
        )
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
