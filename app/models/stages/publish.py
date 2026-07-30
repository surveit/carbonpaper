"""publish stage: the handle config, its artifact-format vocabulary, and
config-column validation — `one_file_per`, when set, must resolve against the
stage's input edge."""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Optional

from app.models.schema import _Base
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns

if TYPE_CHECKING:
    from app.models.stage import Stage


class PublishFormat(str, Enum):
    html_report = "html_report"
    json = "json"
    csv = "csv"
    evidence_cards = "evidence_cards"


class PublishConfig(_Base):
    """publish handle (runs alongside a `function` block)."""
    # Every field changes what this stage computes (format, destination,
    # template, layout) — see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "format", "destination", "template", "one_file_per", "cross_link",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    format: Optional[PublishFormat] = None
    destination: Optional[str] = None
    template: Optional[str] = None
    one_file_per: Optional[str] = None
    cross_link: Optional[bool] = None


def find_publish_column_issues(stage: "Stage") -> list[str]:
    """One issue if `publish.one_file_per` is set and absent from the
    resolved single input."""
    publish = stage.publish
    assert publish is not None  # Stage._handle_for_type guarantees this for type="publish"
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
