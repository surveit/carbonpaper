"""StageTypeSpec: one stage type's authoring copy, typed."""
from __future__ import annotations

from typing import Literal

from app.models.schema import _Base


class StageTypeSpec(_Base):
    summary: str
    signature_form: Literal["extends", "replaces"]
    blocks: list[str]
    requires_inputs: bool
    min_inputs: int
    required: list[str]
    optional: list[str]
    notes: str
