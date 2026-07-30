"""publish stage: the config block, its artifact-format vocabulary, and
config-column validation — `one_file_per`, when set, must resolve against the
stage's input edge."""
from __future__ import annotations

from enum import Enum
from typing import ClassVar, Literal, Optional

from pydantic import Field

from app.models.schema import _Base
from app.models.stage_base import StageBase, StageInput, StageType
from app.models.stages.code import PythonFunction
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns


class PublishFormat(str, Enum):
    html_report = "html_report"
    json = "json"
    csv = "csv"
    evidence_cards = "evidence_cards"


class PublishConfig(_Base):
    """publish rendering config. The code a publish stage RUNS lives in its
    `function` block, not here."""
    # Every field changes what this stage computes (format, destination,
    # template, layout) — see StageBase.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "format", "destination", "template", "one_file_per", "cross_link",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    format: Optional[PublishFormat] = None
    destination: Optional[str] = None
    template: Optional[str] = None
    one_file_per: Optional[str] = None
    cross_link: Optional[bool] = None


class PublishStage(StageBase):
    """The `publish` block is this stage's rendering config; the `function`
    block is the code it actually runs, so both are required and both are
    fingerprinted."""
    # The one type that emits files rather than a table.
    REQUIRES_OUTPUT_SCHEMA: ClassVar[bool] = False

    type: Literal[StageType.publish]
    publish: PublishConfig
    function: PythonFunction
    inputs: list[StageInput] = Field(default_factory=list, min_length=1)

    def fingerprint_blocks(self) -> dict[str, _Base]:
        return {"publish": self.publish, "function": self.function}

    def find_config_column_issues(self) -> list[str]:
        return find_publish_column_issues(self)

    def find_authored_code_block(self) -> PythonFunction:
        return self.function


def find_publish_column_issues(stage: "PublishStage") -> list[str]:
    """One issue if `publish.one_file_per` is set and absent from the
    resolved single input."""
    publish = stage.publish
    if not publish.one_file_per:
        return []
    cols = resolve_input_columns(stage, 0)
    if publish.one_file_per in cols:
        return []
    return [
        COLUMN_ISSUE.format(
            sid=stage.id, field="publish.one_file_per", col=publish.one_file_per, cols=sorted(cols)
        )
    ]
