from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ProjectEdit(BaseModel):
    # A key nothing here can write is a 422 rather than a silently dropped field.
    model_config = ConfigDict(extra="forbid")

    title: str
