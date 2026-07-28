"""Config-column validation for an llm_transform stage: every `{placeholder}`
its prompt template actually interpolates must resolve against the stage's
input edge."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.prompt_template import find_template_fields
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns

if TYPE_CHECKING:
    from app.models.stage import Stage


def find_llm_prompt_column_issues(stage: "Stage") -> list[str]:
    """Every `{placeholder}` the prompt template actually interpolates (per
    `find_template_fields` — the same parser the runtime renders with) that is
    absent from the resolved single input."""
    llm = stage.llm
    assert llm is not None  # Stage._handle_for_type guarantees this for type="llm_transform"
    cols = resolve_input_columns(stage, 0)
    return [
        COLUMN_ISSUE.format(sid=stage.id, field=f"llm prompt {{{field}}}", col=field, cols=sorted(cols))
        for field in sorted(find_template_fields(llm.prompt_data_template))
        if field not in cols
    ]
