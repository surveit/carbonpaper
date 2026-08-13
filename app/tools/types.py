"""Types every tool module shares."""
from __future__ import annotations

# What each of a tool's arguments IS, in prose the model reads: name -> description.
# What each argument TAKES is read off the function, so it is not restated here.
# Empty = the tool's arguments need no explaining, or it takes none.
ToolParameterProse = dict[str, str]
