"""What a stored payload is made of, before anything gives it a shape."""
from __future__ import annotations

from typing import Any

# The one honest dynamic boundary: an arbitrary pydantic model_dump / JSON body.
JsonDict = dict[str, Any]
# What a stored field can be compared against: a JSON scalar, never a list or object.
JsonScalar = str | int | float | bool | None
