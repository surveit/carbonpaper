"""A span cell as a table prints it: its quotes as the cell's text, each span kept to link."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.models.locators import label_locator
from app.models.spans import Span, read_span_cell


@dataclass(frozen=True)
class SpanCite:
    span: Span
    label: str


class SpanCellText(str):
    """Its quotes wherever a table prints a cell as text; `cites` is what `span_cites` links."""

    cites: tuple[SpanCite, ...]

    def __new__(cls, spans: Sequence[Span]) -> SpanCellText:
        text = super().__new__(cls, "; ".join(span.quote for span in spans))
        text.cites = tuple(SpanCite(span, label_locator(span.locator)) for span in spans)
        return text


def render_span_column(values: Sequence[object], texts: Sequence[str]) -> list[str] | None:
    """`texts` with each span cell's text swapped in; None where no cell holds a span."""
    span_texts = [render_span_cell(value) for value in values]
    if all(span_text is None for span_text in span_texts):
        return None
    # `is None`, not falsiness: a span quoting a blank page reads as "".
    return [text if span_text is None else span_text for span_text, text in zip(span_texts, texts)]


def render_span_cell(value: object) -> SpanCellText | None:
    spans = read_span_cell(value)
    return None if spans is None else SpanCellText(spans)
