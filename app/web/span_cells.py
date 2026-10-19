"""A span cell as a table prints it: its quotes as the cell's text, each span kept to link."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.models.locators import PageCharRange, label_locator
from app.models.spans import Span, read_span_cell


@dataclass(frozen=True)
class SpanCite:
    span: Span
    label: str


class SpanCellText(str):
    """Its quotes wherever a table prints a cell as text; `cites` is what `span_cites` links."""

    cites: tuple[SpanCite, ...]
    filenames_by_sha256: Mapping[str, str] | None

    # Given `filenames_by_sha256`, each label also names the file it quotes.
    def __new__(cls, spans: Sequence[Span],
                filenames_by_sha256: Mapping[str, str] | None = None) -> SpanCellText:
        text = super().__new__(cls, "; ".join(span.quote for span in spans))
        text.cites = tuple(SpanCite(span, _label_span(span, filenames_by_sha256))
                           for span in spans)
        text.filenames_by_sha256 = filenames_by_sha256
        return text

    # str's own copy protocol would hand __new__ the quotes; this hands it the spans.
    def __reduce__(
        self,
    ) -> tuple[type[SpanCellText], tuple[list[Span], Mapping[str, str] | None]]:
        return (SpanCellText, ([cite.span for cite in self.cites], self.filenames_by_sha256))

    # The same quote at another address is another cell, so a stage diff marks it changed.
    def __eq__(self, other: object) -> bool:
        return (isinstance(other, SpanCellText) and str(self) == str(other)
                and self.cites == other.cites)

    def __ne__(self, other: object) -> bool:
        return not self == other

    __hash__ = str.__hash__


def render_span_column(values: Sequence[object], texts: Sequence[str]) -> list[str] | None:
    """`texts` with each span cell's text swapped in; None where no cell holds a span."""
    span_texts = [render_span_cell(value) for value in values]
    if all(span_text is None for span_text in span_texts):
        return None
    # `is None`, not falsiness: a span quoting a blank page reads as "".
    return [text if span_text is None else span_text for span_text, text in zip(span_texts, texts)]


def render_span_cell(
    value: object, filenames_by_sha256: Mapping[str, str] | None = None,
) -> SpanCellText | None:
    spans = read_span_cell(value)
    return None if spans is None else SpanCellText(spans, filenames_by_sha256)


def _label_span(span: Span, filenames_by_sha256: Mapping[str, str] | None) -> str:
    if filenames_by_sha256 is None:
        return label_locator(span.locator)
    filename = filenames_by_sha256[span.source_sha256]
    if isinstance(span.locator, PageCharRange):
        return f"p. {span.locator.page} of {filename}"
    return f"{label_locator(span.locator)} of {filename}"
