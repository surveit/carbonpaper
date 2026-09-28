"""A Span: a quote and the address it sits at in one stored Source. narrow_span re-finds a quote."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import Field, SerializeAsAny, ValidationError, field_validator, model_validator

from app.core.errors import QuoteAmbiguous, QuoteNotInText
from app.core.ids import ID
from app.models.base import _Base
from app.models.locators import CharRange, Locator, PageCharRange, label_locator, parse_any_locator

_CHARACTER_RANGES = (PageCharRange, CharRange)


# `prefix` and `suffix` are the verbatim text just before and after `quote`, where one was given.
class Span(_Base):
    source_id: ID
    source_sha256: str
    # SerializeAsAny, or a dump would keep only `kind` and drop the subclass's coordinates.
    locator: SerializeAsAny[Locator]
    quote: str
    prefix: str | None = None
    suffix: str | None = None

    @field_validator("locator", mode="before")
    @classmethod
    def _parse_locator_by_kind(cls, value: object) -> object:
        return parse_any_locator(value)

    @model_validator(mode="after")
    def _range_is_as_long_as_the_quote(self) -> Self:
        locator = self.locator
        if isinstance(locator, _CHARACTER_RANGES) and locator.end - locator.start != len(self.quote):
            raise ValueError(
                f"the {locator.kind} range covers {locator.end - locator.start} characters, "
                f"but the quote has {len(self.quote)}"
            )
        return self


# No docstring: an agent reads this class's JSON schema, where a docstring becomes its description.
class SpanReply(_Base):
    quote: str = Field(
        min_length=1,
        description="Words copied exactly from the text you were given: same spelling, spacing "
        "and punctuation.",
    )
    prefix: str | None = Field(
        default=None,
        description="The text immediately before the quote, copied exactly, including any space "
        "between them. Give it when the quote appears more than once.",
    )
    suffix: str | None = Field(
        default=None,
        description="The text immediately after the quote, copied exactly, including any space "
        "between them. Give it when the quote appears more than once.",
    )


def read_span_cell(value: object) -> list[Span] | None:
    """The spans a span or list[span] cell holds; None for any other cell."""
    elements = value if isinstance(value, list) else [value]
    if not elements or not all(isinstance(element, Mapping) for element in elements):
        return None
    try:
        return [Span.model_validate(element, strict=True) for element in elements]
    except ValidationError:
        return None


def narrow_span(
    parent: Span, quote: str, *, prefix: str | None = None, suffix: str | None = None
) -> Span:
    """Never fuzzy: the quote and its context must match `parent.quote` exactly, at one place."""
    locator = parent.locator
    if not isinstance(locator, _CHARACTER_RANGES):
        raise ValueError(f"a {locator.kind!r} span holds no character offsets to narrow")
    start = locator.start + _find_single_occurrence(parent, quote, prefix, suffix)
    return Span(
        source_id=parent.source_id,
        source_sha256=parent.source_sha256,
        locator=locator.model_copy(update={"start": start, "end": start + len(quote)}),
        quote=quote,
        prefix=prefix,
        suffix=suffix,
    )


def find_occurrences(text: str, quote: str) -> list[int]:
    # Each search starts one past the previous start, so "aa" is found twice in "aaa".
    offsets: list[int] = []
    offset = text.find(quote)
    while offset != -1:
        offsets.append(offset)
        offset = text.find(quote, offset + 1)
    return offsets


def _find_single_occurrence(
    parent: Span, quote: str, prefix: str | None, suffix: str | None
) -> int:
    where = label_locator(parent.locator)
    if not quote:
        raise QuoteNotInText(f"an empty quote names no text in {where}")
    offsets = find_occurrences(parent.quote, quote)
    if not offsets:
        raise QuoteNotInText(f"quote not found in {where}: {quote!r}")
    framed = [
        offset for offset in offsets
        if _is_framed_by(parent.quote, offset, offset + len(quote), prefix, suffix)
    ]
    if not framed:
        raise QuoteNotInText(
            f"quote appears {len(offsets)} time(s) in {where}, none with the prefix and suffix "
            f"given: {quote!r}"
        )
    if len(framed) > 1:
        raise QuoteAmbiguous(
            f"quote appears {len(framed)} times in {where}, and no prefix or suffix tells them "
            f"apart: {quote!r}"
        )
    return framed[0]


def _is_framed_by(
    text: str, start: int, end: int, prefix: str | None, suffix: str | None
) -> bool:
    return (prefix is None or text.endswith(prefix, 0, start)) and (
        suffix is None or text.startswith(suffix, end)
    )
