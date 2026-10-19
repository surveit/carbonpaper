from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# GitHub's own payload, written back out untouched; its shape is GitHub's, never checked here.
UncheckedGitHubJson = dict[str, Any]
# A JSON Schema handed to the model backend, which enforces it on the reply.
JsonSchemaDocument = dict[str, Any]
# A reply the backend checked against a JsonSchemaDocument; the caller parses it into a model.
UncheckedModelAnswer = dict[str, Any]


class ForeignRecord(BaseModel):
    """A record read from another tool's JSON; fields this package never reads are dropped."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class CriticRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
