"""The two tables a pack's chronology workflow publishes, which every chronology view reads."""
from __future__ import annotations

from app.models.schema import Column

DATE_PRECISIONS = ["day", "month", "year", "none"]
DISPUTE_STATUSES = ["undisputed", "disputed", "unreviewed"]

EVENT_COLUMNS: tuple[Column, ...] = (
    # An ISO string: an llm_transform reply and a Starlark return both reach a row as text.
    Column(name="date", type="str", nullable=True,
           description="YYYY-MM-DD. A month- or year-precision date is its first day."),
    Column(name="date_precision", type="str", nullable=False, enum=DATE_PRECISIONS,
           description="How much of `date` the source states."),
    Column(name="speaker", type="str", nullable=False),
    Column(name="topic", type="str", nullable=False),
    Column(name="statement", type="str", nullable=False),
    Column(name="span", type="span", nullable=False),
    Column(name="pin_cite", type="str", nullable=False),
    Column(name="source_kind", type="str", nullable=False,
           description="What kind of record the span quotes, such as `court_filing`."),
)

DISPUTE_COLUMNS: tuple[Column, ...] = (
    Column(name="topic", type="str", nullable=False),
    Column(name="status", type="str", nullable=False, enum=DISPUTE_STATUSES),
    Column(name="summary", type="str", nullable=False),
    Column(name="supporting", type="list[span]", nullable=False),
    Column(name="contrary", type="list[span]", nullable=False),
)
