"""NodeTypeSpec: one stage type's authoring copy, typed."""
from __future__ import annotations

from typing import Literal

from app.models.schema import _Base


class NodeTypeSpec(_Base):
    """What the authoring prompts render for one stage type."""

    summary: str
    transform_signature_form: Literal["extends", "overwrites"]
    blocks: list[str]
    requires_inputs: bool
    min_inputs: int
    required: list[str]
    optional: list[str]
    notes: str
