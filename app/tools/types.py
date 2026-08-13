"""Types every tool module shares."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# What each of a tool's arguments IS, in prose the model reads: name -> description.
# What each argument TAKES is read off the function, so it is not restated here.
# Every argument the model is shown needs an entry; binding refuses one without. So
# empty means the model is shown no argument: the function takes none, or `skip` covers
# every one it takes.
ToolParameterProse = dict[str, str]


@dataclass(frozen=True)
class ToolProse:
    """Everything a surface offering this tool tells the model about it."""

    description: str
    parameters: ToolParameterProse


@dataclass(frozen=True)
class AgentTool(ToolProse):
    """Prose, body and label together, so binding a tool by name is one lookup."""

    fn: Callable[..., Any]
    label: str
