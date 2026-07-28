"""StageDraft — the stage shape an authoring client sends, as opposed to the
stage a workflow stores (`Stage`)."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import Field, field_validator

from app.models.schema import TableSchema, _Base
from app.models.stage import (
    AggregateConfig,
    Connector,
    InputRef,
    JoinConfig,
    LLMConfig,
    PublishConfig,
    PythonFunction,
    QueueConfig,
    StageType,
)


class StageDraft(_Base):
    """One stage as an authoring client submits it: `Stage` minus the fields no
    client writes (`tests`, `eval`, `review`, `source`), and minus every
    cross-field validator — a stage that breaks a rule must reach the handler and
    be refused by `Stage` there, not rejected during parameter binding."""

    id: str
    type: StageType
    name: str
    inputs: list[InputRef] = Field(default_factory=list)
    output_schema: Optional[TableSchema] = None

    connector: Optional[Connector] = None
    llm: Optional[LLMConfig] = None
    function: Optional[PythonFunction] = None
    join: Optional[JoinConfig] = None
    aggregate: Optional[AggregateConfig] = None
    queue: Optional[QueueConfig] = None
    publish: Optional[PublishConfig] = None

    limit: Optional[int] = None
    compiler_notes: list[str] = Field(default_factory=list)

    @field_validator("inputs", mode="before")
    @classmethod
    def _bare_id_shorthand(cls, v: Any) -> Any:
        """Accept `inputs: [upstream_id]` shorthand, as `Stage` does."""
        if not isinstance(v, list):
            return v
        return [{"id": item} if isinstance(item, str) else item for item in v]

    def to_stage_spec(self) -> dict[str, Any]:
        """This draft as a dict `Stage.model_validate` accepts — by alias, so
        `InputRef.table_schema` spells itself `schema:` the way a compiled stage
        does."""
        return self.model_dump(exclude_unset=True, by_alias=True)
