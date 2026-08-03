"""What the model is told about one tool, as a value rather than a docstring.

Deliberately free of any agent-SDK import, so a layer that only DESCRIBES tools
(app.agents.tool_specs) can hold specs without depending on a runtime."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
