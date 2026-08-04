"""publish stage: the config block, its artifact-format vocabulary, and
config-column validation — `one_file_per`, when set, must resolve against the
stage's input edge."""
from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Literal, Optional

from pydantic import Field

from app.models.schema import StageConfig
from app.models.stage_base import StageInput, StageType
from app.models.stages.code import CarriesPythonFunctionStage
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns


class PublishFormat(str, Enum):
    html_report = "html_report"
    json = "json"
    csv = "csv"
    evidence_cards = "evidence_cards"


class PublishConfig(StageConfig):
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


class PublishStage(CarriesPythonFunctionStage):
    """The `publish` block is this stage's rendering config; the `function`
    block is the code it actually runs, so both are required and both are
    fingerprinted."""
    # The one type that emits files rather than a table.
    REQUIRES_OUTPUT_SCHEMA: ClassVar[bool] = False

    type: Literal[StageType.publish]
    publish: PublishConfig
    inputs: list[StageInput] = Field(default_factory=list, min_length=1)

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"publish": self.publish, **super().fingerprint_blocks()}

    def find_config_column_issues(self) -> list[str]:
        return find_publish_column_issues(self)


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

# Authoring notes for this module's stage type(s), as the plain-data shape the
# authoring prompts render. Assembled into NODE_TYPES by app.models.stages.
NODE_TYPE_SPECS: dict[str, dict[str, Any]] = {
    "publish": {
        "summary": "Render a final artifact (html, json, csv, cards).",
        "blocks": ["publish", "function"],
        "requires_inputs": True,
        "min_inputs": 1,
        "required": [],
        "optional": ["format", "destination", "template", "one_file_per", "cross_link"],
        "notes": (
            "Published output must be INTERROGABLE: every row or claim it renders links "
            "back to that row's provenance. Declare the keyword `trace_links` on the "
            "function — `def transform(df, output_dir, trace_links)` — and the runtime "
            "hands it a linker for this run; per row emit "
            "`trace_links.build_row_trace_url(\"<the input stage's id>\", row_ordinal)` as "
            "an href, where row_ordinal is that row's 0-based position in the input frame "
            "AS RECEIVED. Iterate the frame in order (enumerate it) and do not sort, "
            "filter, or dedup before reading the ordinal — position is the only key the "
            "trace has. Omit the keyword for a format that cannot carry a link (csv, json). "
            "The one type exempt from declaring an output_schema: it emits files, not a table."
        ),
    },
}
