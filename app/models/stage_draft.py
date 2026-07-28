"""StageDraft — the stage shape an authoring client sends, as opposed to the
stage a workflow stores (`Stage`)."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import ConfigDict, Field, field_validator

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


def strip_titles(schema: dict[str, Any]) -> None:
    """Drop every Pydantic-generated `title` from `schema`, in place and at every
    depth.

    Two call sites, because neither alone covers the whole document: as
    StageDraft's `model_config["json_schema_extra"]` it runs on that model's own
    object (its `title` and its properties' titles) and so travels with the model
    when another schema embeds it — but Pydantic hoists `$defs` after the hook
    runs, so a nested model's titles are still there; StageDraft.model_json_schema
    calls it again on the finished document to reach those. As a per-field
    `Field(json_schema_extra=...)` it does NOT work at all: Pydantic re-adds the
    field's title after the callable returns."""
    schema.pop("title", None)
    for value in schema.values():
        if isinstance(value, dict):
            strip_titles(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    strip_titles(item)


class StageDraft(_Base):
    """One stage as an authoring client submits it: `Stage` minus the fields no
    client writes (`tests`, `eval`, `review`, `source`), and minus every
    cross-field validator — a stage that breaks a rule must reach the handler and
    be refused by `Stage` there, not rejected during parameter binding."""

    model_config = ConfigDict(json_schema_extra=strip_titles)

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

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        schema: dict[str, Any] = super().model_json_schema(*args, **kwargs)
        strip_titles(schema)
        return schema

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
