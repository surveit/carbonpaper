"""Types every tool module shares."""
from __future__ import annotations

# A tool's parameters as the SDK wants them: name -> a plain type, or an
# Annotated[type, "description"] it turns into the JSON Schema the CLI sees.
# Empty = the tool takes no arguments.
ToolInputSchema = dict[str, object]
