"""`_Base`: the pydantic model every app.models contract extends. It refuses fields it does not name."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Base(BaseModel):
    """An enum-typed field holds a plain string after validation: compare with `==`, not `is`."""
    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
        populate_by_name=True,
    )
